"""Node-RED room devices. Commands acknowledge requests; GET confirms reality."""
import asyncio
import json
import time
import urllib.request


class RoomDevices:
    def __init__(self, config=None):
        self.config = config or {}
        self.base = self.config.get('base_url', '').rstrip('/')
        self.snapshot = {'enabled': bool(self.base), 'beamer': [], 'error': None,
                         'updated_at': None}
        self.lock = asyncio.Lock()

    async def _request(self, path, method='GET'):
        def request():
            req = urllib.request.Request(self.base + path, method=method,
                                         data=b'' if method == 'POST' else None)
            with urllib.request.urlopen(req, timeout=5) as response:
                body = response.read(262144)
                return json.loads(body) if method == 'GET' else None
        # Cancelling an asyncio waiter does not stop its HTTP worker thread.
        # Keep the command lock until the worker settles, so a cancelled On
        # request cannot arrive after a subsequent Sleep/Off request.
        worker = asyncio.create_task(asyncio.to_thread(request))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                if worker.cancelled():
                    raise
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def refresh(self):
        if not self.base:
            return self.snapshot
        async with self.lock:
            try:
                data = await self._request('/projector/dortmund/status')
                if not isinstance(data, dict) or not isinstance(data.get('beamer'), list):
                    raise ValueError('invalid projector status response')
                self.snapshot = {**data, 'enabled': True, 'error': None,
                                 'updated_at': time.time()}
            except Exception as exc:
                self.snapshot = {**self.snapshot, 'error': str(exc)}
        return self.snapshot

    async def command(self, target, action):
        if not self.base:
            raise RuntimeError('Room device API is not configured')
        if target in ('panorama', 'praesi') and action in ('on', 'off'):
            path = f'/projector/dortmund/{target}/{action}'
        elif target.startswith('beamer/') and target[7:] in {str(n) for n in range(1, 14)} and action in ('on', 'off'):
            path = f'/projector/dortmund/{target}/{action}'
        elif target in ('curtain/1', 'curtain/2') and action in ('auf', 'zu'):
            path = f'/curtain/dortmund/{target[8:]}/{action}'
        else:
            raise ValueError('Unknown room device or action')
        async with self.lock:
            await self._request(path, 'POST')
        # A successful POST is only an acknowledgement, never an "on" status.
        return {'accepted': True, 'target': target, 'action': action}

    def blockers(self):
        if self.snapshot.get('error'):
            return ['projector status unavailable: ' + self.snapshot['error']]
        if not self.snapshot.get('updated_at') or time.time() - self.snapshot['updated_at'] > 10:
            return ['projector status is stale']
        by_number = {p.get('nr'): p for p in self.snapshot['beamer']}
        return [f"{n}: {by_number.get(n, {}).get('ist', 'unknown')}"
                for n in range(1, 13)
                if not (by_number.get(n, {}).get('antwortet') is True
                        and by_number[n].get('ist') == 'an'
                        and not by_number[n].get('fehler'))]

    async def ready(self, notify):
        if not self.base:
            return
        await self.command('praesi', 'off')
        await self.command('panorama', 'on')
        deadline = asyncio.get_running_loop().time() + self.config.get('ready_timeout_s', 180)
        while True:
            await self.refresh()
            await notify({'type': 'room_devices', 'devices': self.snapshot})
            waiting = self.blockers()
            if not waiting:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError('Projectors not ready: ' + ', '.join(waiting))
            await asyncio.sleep(2)
