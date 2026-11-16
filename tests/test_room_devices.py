import asyncio
import time
import pytest
from room_devices import RoomDevices


def status():
    return {'beamer': [{'nr': n, 'ist': 'an', 'antwortet': True, 'fehler': ''}
                       for n in range(1, 14)]}


def test_readiness_uses_actual_not_requested_state():
    devices = RoomDevices({'base_url': 'http://example'})
    devices.snapshot = {**status(), 'updated_at': time.time(), 'error': None}
    assert devices.blockers() == []
    devices.snapshot['beamer'][3].update(ist='aus', ziel='an')
    assert devices.blockers() == ['4: aus']
    devices.snapshot['updated_at'] -= 20
    assert devices.blockers() == ['projector status is stale']


def test_ready_waits_for_all_twelve_and_not_presentation():
    async def run():
        devices = RoomDevices({'base_url': 'http://example'})
        calls = []
        async def request(path, method='GET'):
            calls.append((path, method))
            result = status()
            result['beamer'][12]['ist'] = 'aus'
            return result
        devices._request = request
        messages = []
        async def notify(message):
            messages.append(message)
        await devices.ready(notify)
        assert calls == [('/projector/dortmund/praesi/off', 'POST'),
                         ('/projector/dortmund/panorama/on', 'POST'),
                         ('/projector/dortmund/status', 'GET')]
        assert messages[0]['devices']['beamer'][12]['ist'] == 'aus'
        with pytest.raises(ValueError):
            await devices.command('beamer/99', 'on')
    asyncio.run(run())


def test_failure_preserves_last_status_but_blocks_playback():
    async def run():
        devices = RoomDevices({'base_url': 'http://example'})
        devices.snapshot = {**status(), 'updated_at': time.time(), 'error': None}
        async def request(*args):
            raise OSError('offline')
        devices._request = request
        await devices.refresh()
        assert len(devices.snapshot['beamer']) == 13
        assert 'offline' in devices.blockers()[0]
        with pytest.raises(OSError):
            await devices.command('panorama', 'on')
    asyncio.run(run())


def test_sleep_cancels_waiting_play_and_keeps_nodes_in_standby(tmp_path):
    from controller import Controller
    from roommodel import RoomModel
    async def run():
        media = tmp_path / 'media'
        media.mkdir()
        (media / 'test.mp4').touch()
        room = RoomModel(tmp_path / 'room.json')
        room.model['room_devices'] = {'base_url': 'http://example'}
        ctl = Controller(room, 0, 0, 0, media)
        messages, commands = [], []
        async def broadcast(message):
            messages.append(message)
        ctl._broadcast_nodes = broadcast
        ctl._broadcast_web = broadcast
        entered = asyncio.Event()
        async def ready(notify):
            entered.set()
            await asyncio.Event().wait()
        async def command(target, action):
            commands.append((target, action))
        ctl.devices.ready = ready
        ctl.devices.command = command
        start = ctl._start_task = asyncio.create_task(ctl._play_request('test.mp4'))
        await entered.wait()
        await asyncio.wait_for(ctl.cmd_hibernate(), 1)
        assert start.cancelled()
        assert commands == [('panorama', 'off'), ('praesi', 'off')]
        assert {'cmd': 'sleep', 'poweroff': False, 'standby': True} in messages
        assert ctl.show['playing'] is False
    asyncio.run(run())


def test_cancelled_http_on_finishes_before_sleep_off(monkeypatch):
    import threading
    import urllib.request
    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        events = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return b'{}'
        def urlopen(request, timeout):
            action = request.full_url.rsplit('/', 1)[1]
            events.append(action + '-start')
            if action == 'on':
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(2)
            events.append(action + '-finish')
            return Response()
        monkeypatch.setattr(urllib.request, 'urlopen', urlopen)
        devices = RoomDevices({'base_url': 'http://fake'})
        on = asyncio.create_task(devices.command('panorama', 'on'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            on.cancel()
            await asyncio.sleep(0)
            on.cancel()  # Stop and Sleep may both cancel the pending start.
            off = asyncio.create_task(devices.command('panorama', 'off'))
            await asyncio.sleep(.02)
            assert events == ['on-start']
            assert not off.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await on
        await asyncio.wait_for(off, 1)
        assert events == ['on-start', 'on-finish', 'off-start', 'off-finish']
    asyncio.run(run())


@pytest.mark.parametrize('operation', ['play', 'wake'])
def test_stop_cancels_direct_http_start_and_wake(tmp_path, operation):
    from controller import Controller
    from roommodel import RoomModel
    async def run():
        media = tmp_path / 'media'
        media.mkdir()
        (media / 'test.mp4').touch()
        room = RoomModel(tmp_path / 'room.json')
        room.model['room_devices'] = {'base_url': 'http://fake'}
        ctl = Controller(room, 0, 0, 0, media)
        async def ignore(message): pass
        ctl._broadcast_nodes = ctl._broadcast_web = ignore
        entered = asyncio.Event()
        async def ready(notify):
            entered.set()
            await asyncio.Event().wait()
        ctl.devices.ready = ready
        # HTTP calls these methods directly, without the WS _play_request task.
        start = asyncio.create_task(ctl.cmd_play_media('test.mp4') if operation == 'play' else ctl.cmd_wake())
        await asyncio.wait_for(entered.wait(), 1)
        assert ctl._start_task is start
        await ctl.cmd_stop()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start, 1)
        assert ctl._start_task is None
        assert not ctl.show['playing']
    asyncio.run(run())
