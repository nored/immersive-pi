#!/usr/bin/env python3
"""Prepare the room-device backport without upgrading the live v1 renderer protocol.

Input is a backup of controller.py, index.html and app.js from production.
Remove this compatibility step when promoting the tested v2 controller.
"""
import argparse
import ast
from prepare_no_preview_backport import strip_ui, status_ui
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('original', type=Path)
p.add_argument('output', type=Path)
a = p.parse_args()
root = Path(__file__).resolve().parents[1]
source = (root / 'control-node/controller.py').read_text()
s = (a.original / 'controller.py').read_text()
if 'class FrameCoordinator' in s or 'from shared.frame_sync' in s or 'RoomDevices' in s:
    raise SystemExit('Expected an unmodified legacy controller backup')
a.output.mkdir(parents=True, exist_ok=True)
s = s.replace('import powerctl\n', 'import powerctl\nfrom room_devices import RoomDevices\nfrom start_operation import managed_start\n', 1)
s = s.replace('        self.power_state = "awake"', '        self.devices = RoomDevices(room.model.get("room_devices"))\n        self._room_operation = asyncio.Lock()\n        self._start_task = None\n        self.power_state = "awake"', 1)
s = s.replace('"power_state": self.power_state,', '"power_state": self.power_state,\n                              "room_devices": self.devices.snapshot,')
s = s.replace('        elif cmd == "play_media":\n            await self.cmd_play_media(msg.get("media"))', '        elif cmd == "play_media":\n            if self._start_task:\n                self._start_task.cancel()\n            self._start_task = asyncio.create_task(self._play_request(msg.get("media")))', 1)
s = s.replace('        elif cmd == "play":\n            await self.cmd_play()', '        elif cmd == "play":\n            if self._start_task:\n                self._start_task.cancel()\n            self._start_task = asyncio.create_task(self._play_request(self.show.get("media")))', 1)
branch = source[source.index('        elif cmd == "room_device":'):source.index('        elif cmd == "save":')]
s = s[:s.index('        elif cmd == "hibernate":')] + branch + s[s.index('        elif cmd == "save":'):]
s = s.replace('    async def cmd_hibernate(self):', '    async def _hibernate_nodes(self):', 1)
s = s.replace('    async def cmd_wake(self):', '    async def _wake_nodes(self):', 1)
s = s.replace('    async def cmd_play_media(self, media: str) -> dict:', '    async def _legacy_play_media(self, media: str) -> dict:', 1)
s = s.replace('    async def cmd_stop(self):', '''    async def cmd_stop(self):
        if self._start_task and self._start_task is not asyncio.current_task():
            task = self._start_task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
''', 1)
# Explicit Start resumes playback; waking alone must not silently replay old media.
s = s.replace('        if self._was_playing:\n            await self.cmd_prepare(None, None)\n            await asyncio.sleep(0.5)\n            await self.cmd_play()\n', '')
methods = ast.parse(source).body
controller = next(n for n in methods if isinstance(n, ast.ClassDef) and n.name == 'Controller')
for name in ('cmd_hibernate', 'cmd_wake', '_wake_request', '_device_status_loop'):
    method = next(n for n in controller.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    text = '\n'.join(source.splitlines()[method.lineno-1:method.end_lineno]) + '\n\n'
    if name == 'cmd_wake':
        text = '    @managed_start\n' + text
    s = s.replace('    # ---- plumbing ', text + '    # ---- plumbing ', 1)
extra = '''    async def _play_request(self, media):
        try:
            await self.cmd_play_media(media)
        except Exception as exc:
            self.show.update(playing=False, state="error", error=str(exc))
            await self._broadcast_web({"type": "error", "message": str(exc)})

    @managed_start
    async def cmd_play_media(self, media):
        await self.cmd_stop()
        media = media or self.show.get("media") or self.room.model.get("media", "")
        if str(media).startswith("native-ring") or str(media).endswith(".immersive.json"):
            raise RuntimeError("Per-projector tiles require the updated renderer. Legacy playback is blocked to prevent the previous crash regression.")
        async with self._room_operation:
            self.show.update(playing=False, state="waiting_projectors")
            await self._broadcast_web({"type": "show", "show": self.show})
            await self.devices.ready(self._broadcast_web)
            await self._wake_nodes()
            return await self._legacy_play_media(media)

'''
s = s.replace('            \"was_playing\": self._was_playing,', '            \"was_playing\": self._was_playing,\n            \"room_devices\": self.devices.snapshot,', 1)
s = s.replace('    # ---- plumbing ', extra + '    # ---- plumbing ', 1)
s = s.replace('asyncio.create_task(self._mdns_browse_loop())]', 'asyncio.create_task(self._mdns_browse_loop()),\n                     asyncio.create_task(self._device_status_loop())]')
s = s.replace('            # forward preview JPEG straight to all web clients\n            await self._broadcast_web(msg)\n            return', '            # Discard legacy images; retain telemetry.\n            return', 1)
s = s.replace('\"avahi-browse\", \"-rptk\"', '\"avahi-browse\", \"-rpt\"')
compile(s, 'controller.py', 'exec')
(a.output / 'controller.py').write_text(s)
(a.output / 'room_devices.py').write_text((root / 'control-node/room_devices.py').read_text())
(a.output / 'start_operation.py').write_text((root / 'control-node/start_operation.py').read_text())
html = (a.original / 'index.html').read_text()
newhtml = (root / 'control-node/web/index.html').read_text()
panel = newhtml[newhtml.index('<details id="room-devices"'):newhtml.index('    <h2>Nodes</h2>')]
html = html.replace('<h2>Nodes</h2>', panel + '<h2>Nodes</h2>', 1)
html = html.replace('>Play</button>', '>Start / Play</button>', 1)
html = html.replace('Sleep the room (power down)', 'Stop video and switch off room projectors')
html = html.replace('</body>',  '<script src="room-devices.js?v=20261115"></script>\n</body>')
html = html.replace('      <img id="backdrop" alt="live preview" />\n', '')
html = html.replace('app.js?v=status-20261114', 'app.js?v=no-preview-20261115')
(a.output / 'index.html').write_text(html)
(a.output / 'app.js').write_text(status_ui(strip_ui((a.original / 'app.js').read_text()), (root / 'control-node/web/app.js').read_text()))
js = (root / 'control-node/web/app.js').read_text()
js = js[js.index('function renderDevices(devices)'):]
js += '''\nconst roomDevicesOriginalMessage = onMessage;
onMessage = function(message) {
  if (message.type === "snapshot") renderDevices(message.room_devices);
  if (message.type === "room_devices") renderDevices(message.devices);
  if (message.type === "error") {
    document.getElementById("device-status").textContent = message.message;
    flash(message.message);
  }
  if (message.type === "show" && message.show.state === "waiting_projectors") {
    document.getElementById("device-status").textContent = "Waiting for all 12 panorama projectors…";
  }
  roomDevicesOriginalMessage(message);
};\n'''
(a.output / 'room-devices.js').write_text(js)
