"""controller.py — show control + heartbeat collector + WebSocket broker.

One asyncio WebSocket server speaks to two kinds of client:
  * render nodes  (role=node) — receive commands, send 1 Hz heartbeats and
    ~8 fps preview JPEGs;
  * web clients   (role=web)  — the calibration site: send edit/show commands,
    receive heartbeats + previews.

The controller also reads the master media clock (same GstSystemClock the
NetTimeProvider publishes) so it can hand out a `play_at` base time a few
hundred ms in the future, letting every node arm and fire together.

Static web assets are served over plain HTTP on a second port so a tablet can
load the site, then upgrade to WebSocket for live control.

Single-command synced playback:
    python3 controller.py --autoplay
brings up the clock, waits for the room-model's nodes to connect, then issues
prepare + play_at automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import powerctl

try:
    import websockets
    ConnClosed = websockets.ConnectionClosed
except Exception:  # importable on a dev box without the runtime dep
    websockets = None
    ConnClosed = Exception

from roommodel import RoomModel

WEB_DIR = Path(__file__).with_name("web")
LEAD_NS = 300_000_000  # 300 ms arm lead for play_at


TEST_VIDEO = "test.mp4"


def ensure_test_video(media_dir: Path):
    """Guarantee a built-in test clip is always available to play, even before
    anyone uploads anything. Ships in the repo/image; regenerated with ffmpeg if
    somehow missing."""
    dst = media_dir / TEST_VIDEO
    if dst.exists():
        return
    import shutil, subprocess
    # if the repo's test-media has a pan clip, reuse it; else synthesize one
    repo_clip = Path(__file__).resolve().parent.parent / "test-media" / "pan.mp4"
    if repo_clip.exists():
        shutil.copy(repo_clip, dst)
        return
    if shutil.which("ffmpeg"):
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi",
                 "-i", "testsrc=size=1280x720:rate=30:duration=10",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dst)],
                check=True, capture_output=True, timeout=120)
        except Exception as e:
            print(f"[ctl] could not generate test video: {e}")


def now_ns() -> int:
    """'Now' on the master media clock, in nanoseconds."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        if not Gst.is_initialized():
            Gst.init(None)
        return Gst.SystemClock.obtain().get_time()
    except Exception:
        # No GStreamer (e.g. dev box) — fall back to monotonic ns so the
        # control logic still runs and can be tested off-Pi.
        import time
        return time.monotonic_ns()


class Controller:
    def __init__(self, room: RoomModel, ws_port: int, clock_port: int,
                 http_port: int, media_dir: Optional[Path] = None):
        self.room = room
        self.ws_port = ws_port
        self.clock_port = clock_port
        self.http_port = http_port
        self.media_dir = Path(media_dir) if media_dir else \
            Path(__file__).resolve().parent / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        ensure_test_video(self.media_dir)

        self.nodes: dict[str, websockets.WebSocketServerProtocol] = {}
        self.webs: set[websockets.WebSocketServerProtocol] = set()
        self.heartbeats: dict[str, dict] = {}
        self.show: dict = {
            "show": room.model.get("show", ""),
            "media": room.model.get("media", ""),
            "playing": False,
            "base_time_ns": None,
            "loop_epoch_ns": None,
        }
        # Hibernation / Node-RED power control.
        self.power = powerctl.make(room.model.get("power"))
        self.power_cfg = room.model.get("power", {}) or {}
        self.power_state = "awake"          # awake | hibernating
        self._was_playing = False
        self.api_token = os.environ.get("IMMERSIVE_API_TOKEN", "")
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ---- client lifecycle ------------------------------------------------
    async def handler(self, ws):
        try:
            raw = await ws.recv()
            hello = json.loads(raw)
        except Exception:
            await ws.close()
            return
        role = hello.get("role")
        if role == "node":
            await self._serve_node(ws, hello.get("hello", "unknown"))
        else:
            await self._serve_web(ws)

    async def _serve_node(self, ws, node: str):
        self.nodes[node] = ws
        print(f"[ctl] node {node} connected ({len(self.nodes)} up)")
        # On connect, push this node its own room-model entry so it warps
        # correctly immediately, before any show starts.
        await self._push_entry(node)
        # If a show is already playing, arm the late joiner onto the same base
        # time and loop epoch — this is also the node-swap path: a replaced Pi
        # boots, connects, and falls straight into the running ring.
        if self.show["playing"] and self.show["base_time_ns"] is not None:
            await self._send(ws, {"cmd": "prepare", "show": self.show["show"],
                                  "slice": self.show["media"]})
            await self._send(ws, {"cmd": "play_at",
                                  "base_time_ns": self.show["base_time_ns"],
                                  "loop_epoch_ns": self.show.get("loop_epoch_ns",
                                                                 self.show["base_time_ns"]),
                                  "clock_port": self.clock_port})
        try:
            async for raw in ws:
                await self._on_node_msg(node, raw)
        except ConnClosed:
            pass
        finally:
            self.nodes.pop(node, None)
            self.heartbeats.pop(node, None)
            print(f"[ctl] node {node} disconnected ({len(self.nodes)} up)")

    async def _serve_web(self, ws):
        self.webs.add(ws)
        # Send a snapshot so a freshly loaded tablet has the full model + state.
        await self._send(ws, {"type": "snapshot",
                              "room": self.room.model,
                              "show": self.show,
                              "heartbeats": self.heartbeats,
                              "nodes_up": list(self.nodes.keys()),
                              "playlist": self.playlist(),
                              "power_state": self.power_state})
        try:
            async for raw in ws:
                await self._on_web_msg(raw)
        except ConnClosed:
            pass
        finally:
            self.webs.discard(ws)

    # ---- inbound from nodes ---------------------------------------------
    async def _on_node_msg(self, node: str, raw):
        msg = json.loads(raw)
        if "preview" in msg:
            # forward preview JPEG straight to all web clients
            await self._broadcast_web(msg)
            return
        if "node" in msg and "clock_offset_ns" in msg:
            self.heartbeats[node] = msg
            off_ms = msg["clock_offset_ns"] / 1e6
            print(f"[hb] {node:6} off={off_ms:+8.3f}ms "
                  f"pos={msg.get('media_pos_ns', 0)/1e9:7.3f}s "
                  f"dec={'ok' if msg.get('decoder_ok') else 'BAD'} "
                  f"fb={'ok' if msg.get('fb_ok') else 'BAD'} "
                  f"{msg.get('temp_c', 0):.1f}C")
            await self._broadcast_web({"type": "heartbeat", "hb": msg})

    # ---- inbound from web (calibration site) ----------------------------
    async def _on_web_msg(self, raw):
        msg = json.loads(raw)
        cmd = msg.get("cmd")
        if cmd in ("set_mesh", "set_blend", "set_color", "set_source_region"):
            await self._edit(cmd, msg)
        elif cmd == "mesh_delta":
            await self._mesh_delta(msg)
        elif cmd == "set_mesh_resolution":
            entry = self.room.set_mesh_resolution(msg["node"], msg["cols"], msg["rows"])
            await self._send_node(msg["node"], {"cmd": "set_mesh", "node": msg["node"],
                                                "mesh": entry["mesh"]})
            await self._broadcast_web({"type": "entry", "entry": entry})
        elif cmd == "testpattern":
            await self._send_node(msg["node"], msg)
        elif cmd == "identify":
            await self._send_node(msg["node"], msg)
        elif cmd == "prepare":
            await self.cmd_prepare(msg.get("show"), msg.get("media"))
        elif cmd == "play":
            await self.cmd_play()
        elif cmd == "stop":
            await self.cmd_stop()
        elif cmd == "blackout":
            if msg.get("node"):
                await self._send_node(msg["node"], {"cmd": "blackout", "on": msg.get("on", True)})
            else:
                await self.cmd_blackout(msg.get("on", True))
        elif cmd == "scan":
            # structured-light: tell one node to show a raw gray-code bitplane
            await self._send_node(msg["node"], {
                "cmd": "scan_show", "axis": msg.get("axis"), "bit": msg.get("bit"),
                "inverted": msg.get("inverted", False), "on": msg.get("on", True)})
        elif cmd == "play_media":
            await self.cmd_play_media(msg.get("media"))
        elif cmd == "list_media":
            await self._broadcast_web({"type": "playlist", "playlist": self.playlist()})
        elif cmd == "add_node":
            self.room.ensure_node(msg["node"], msg.get("projector", ""))
            if msg.get("ip") or msg.get("mac"):
                self.room.set_node_net(msg["node"], msg.get("ip"), msg.get("mac"))
            self.room.save(f"add node {msg['node']}")
            await self._push_entry(msg["node"])
            await self._broadcast_room()
        elif cmd == "remove_node":
            if self.room.remove_node(msg["node"]):
                self.room.save(f"remove node {msg['node']}")
                await self._broadcast_room()
        elif cmd == "set_node_net":
            self.room.set_node_net(msg["node"], msg.get("ip"), msg.get("mac"),
                                   msg.get("projector"))
            self.room.save(f"set net {msg['node']}")
            await self._broadcast_room()
        elif cmd == "hibernate":
            await self.cmd_hibernate()
        elif cmd == "wake":
            await self.cmd_wake()
        elif cmd == "save":
            self.room.save(msg.get("message", "calibration save"))
            await self._broadcast_web({"type": "saved",
                                       "version": self.room.model["version"]})

    async def _edit(self, cmd: str, msg: dict):
        field = {"set_mesh": "mesh", "set_blend": "blend",
                 "set_color": "color", "set_source_region": "source_region"}[cmd]
        node = msg["node"]
        value = msg[field]
        self.room.update_field(node, field, value)
        # live: node updates its shader and the next preview frame + the wall
        await self._send_node(node, {"cmd": cmd, "node": node, field: value})
        await self._broadcast_web({"type": "entry", "entry": self.room.entry_for(node)})

    async def _mesh_delta(self, msg: dict):
        node = msg["node"]
        entry = self.room.apply_mesh_delta(node, msg["index"], msg["x"], msg["y"])
        await self._send_node(node, {"cmd": "set_mesh", "node": node, "mesh": entry["mesh"]})
        await self._broadcast_web({"type": "entry", "entry": entry})

    # ---- show control ----------------------------------------------------
    async def cmd_prepare(self, show: Optional[str], media: Optional[str]):
        self.show["show"] = show or self.room.model.get("show", "phase0")
        self.show["media"] = media or self.room.model.get("media", "pan.mp4")
        await self._broadcast_nodes({"cmd": "prepare", "show": self.show["show"],
                                     "slice": self.show["media"]})
        print(f"[ctl] prepared show={self.show['show']} media={self.show['media']}")

    async def cmd_play(self):
        base = now_ns() + LEAD_NS
        self.show["base_time_ns"] = base
        # The loop epoch is the play base time: every node derives the same
        # target media position as (clock_now - epoch) mod clip_duration, so 12
        # loops stay phase-aligned over a long run instead of fanning apart.
        self.show["loop_epoch_ns"] = base
        self.show["playing"] = True
        await self._broadcast_nodes({"cmd": "play_at", "base_time_ns": base,
                                     "loop_epoch_ns": base,
                                     "clock_port": self.clock_port})
        print(f"[ctl] play_at base={base} (lead {LEAD_NS/1e6:.0f}ms) "
              f"to {len(self.nodes)} nodes")
        await self._broadcast_web({"type": "show", "show": self.show})

    async def cmd_play_media(self, media: str) -> dict:
        """Select a video and start it synced across all nodes — the action
        behind the Node-RED POST /api/play endpoint."""
        media = media or self.room.model.get("media", "pan.mp4")
        await self.cmd_prepare(self.show.get("show", "show"), media)
        await asyncio.sleep(0.3)
        await self.cmd_play()
        return {"playing": True, "media": media, "nodes": list(self.nodes.keys())}

    def playlist(self) -> dict:
        """Videos available to play (from the control node's media dir)."""
        vids = []
        if self.media_dir.exists():
            vids = sorted(p.name for p in self.media_dir.iterdir()
                          if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".h264"))
        return {"media_dir": str(self.media_dir), "videos": vids,
                "current": self.show.get("media"), "playing": self.show.get("playing")}

    async def cmd_stop(self):
        self.show["playing"] = False
        await self._broadcast_nodes({"cmd": "stop"})
        await self._broadcast_web({"type": "show", "show": self.show})
        print("[ctl] stop")

    async def cmd_blackout(self, on: bool):
        await self._broadcast_nodes({"cmd": "blackout", "on": on})
        print(f"[ctl] blackout {'on' if on else 'off'}")

    # ---- hibernation (driven by Node-RED via the REST API) ---------------
    async def cmd_hibernate(self):
        """Put the room to sleep with a real power-down. Render nodes blank and
        clean-poweroff themselves; then the configured power backend cuts their
        power. The control node stays up to receive the wake call."""
        if self.power_state == "hibernating":
            return {"state": "hibernating", "note": "already hibernating"}
        self._was_playing = self.show.get("playing", False)
        self.power_state = "hibernating"
        self.show["playing"] = False
        # 1) tell every render node to blank and power itself off cleanly
        await self._broadcast_nodes({"cmd": "sleep", "poweroff": True})
        await self._broadcast_web({"type": "power", "state": "hibernating"})
        # 2) give them a moment to halt, then cut switched power if we have it
        grace = float(self.power_cfg.get("poweroff_grace_s", 8))
        asyncio.create_task(self._cut_power_after(grace))
        print(f"[ctl] HIBERNATE: {len(self.nodes)} nodes -> poweroff, "
              f"power backend={self.power_cfg.get('backend', 'none')}")
        return {"state": "hibernating", "nodes": list(self.nodes.keys()),
                "backend": self.power_cfg.get("backend", "none"),
                "can_wake": self.power.can_wake}

    async def _cut_power_after(self, grace: float):
        await asyncio.sleep(grace)
        for node in self.room.nodes():
            await self.power.power_off(node)

    async def cmd_wake(self):
        """Wake the room: restore switched power so the nodes cold-boot, wait for
        them to rejoin, then resume the show if it was playing."""
        if self.power_state == "awake":
            return {"state": "awake", "note": "already awake"}
        if not self.power.can_wake:
            return {"state": "hibernating", "ok": False,
                    "note": "power backend cannot wake nodes remotely; restore "
                            "power physically (no WoL on Pi 4). Nodes auto-rejoin."}
        for node in self.room.nodes():
            await self.power.power_on(node)
        self.power_state = "awake"
        await self._broadcast_web({"type": "power", "state": "waking"})
        wait_s = float(self.power_cfg.get("wake_wait_s", 45))
        asyncio.create_task(self._resume_after_wake(wait_s))
        print(f"[ctl] WAKE: restoring power, waiting up to {wait_s:.0f}s for nodes")
        return {"state": "waking", "wake_wait_s": wait_s,
                "resume_play": self._was_playing}

    async def _resume_after_wake(self, wait_s: float):
        want = set(self.room.nodes())
        deadline = wait_s
        while deadline > 0 and not want.issubset(self.nodes.keys()):
            await asyncio.sleep(1.0)
            deadline -= 1.0
        await self._broadcast_web({"type": "power", "state": "awake"})
        if self._was_playing:
            await self.cmd_prepare(None, None)
            await asyncio.sleep(0.5)
            await self.cmd_play()
        print(f"[ctl] awake: {len(self.nodes)}/{len(want)} nodes back"
              f"{' , show resumed' if self._was_playing else ''}")

    def power_status(self) -> dict:
        return {
            "state": self.power_state,
            "backend": self.power_cfg.get("backend", "none"),
            "can_wake": self.power.can_wake,
            "nodes_up": list(self.nodes.keys()),
            "was_playing": self._was_playing,
        }

    # ---- plumbing --------------------------------------------------------
    async def _push_entry(self, node: str):
        entry = self.room.entry_for(node)
        if not entry:
            return
        for cmd, field in (("set_source_region", "source_region"),
                           ("set_mesh", "mesh"), ("set_blend", "blend"),
                           ("set_color", "color")):
            await self._send_node(node, {"cmd": cmd, "node": node, field: entry[field]})

    async def _send(self, ws, obj: dict):
        await ws.send(json.dumps(obj))

    async def _send_node(self, node: str, obj: dict):
        ws = self.nodes.get(node)
        if ws:
            try:
                await ws.send(json.dumps(obj))
            except ConnClosed:
                self.nodes.pop(node, None)

    async def _broadcast_nodes(self, obj: dict):
        data = json.dumps(obj)
        for ws in list(self.nodes.values()):
            try:
                await ws.send(data)
            except ConnClosed:
                pass

    async def _broadcast_room(self):
        """Push the full model + playlist after a roster/network change so every
        open tablet re-renders the node list."""
        await self._broadcast_web({"type": "room", "room": self.room.model,
                                   "nodes_up": list(self.nodes.keys()),
                                   "playlist": self.playlist()})

    async def _broadcast_web(self, obj: dict):
        data = json.dumps(obj)
        for ws in list(self.webs):
            try:
                await ws.send(data)
            except ConnClosed:
                self.webs.discard(ws)

    # ---- autoplay + CLI --------------------------------------------------
    async def autoplay(self):
        want = set(self.room.nodes())
        print(f"[ctl] autoplay waiting for nodes {sorted(want)} ...")
        while not want.issubset(self.nodes.keys()):
            await asyncio.sleep(0.5)
        await asyncio.sleep(0.5)
        await self.cmd_prepare(None, None)
        await asyncio.sleep(0.5)
        await self.cmd_play()

    async def cli(self):
        """Type commands on stdin: prepare / play / stop / blackout / nodes / quit."""
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, _read_line)
            if line is None:
                break
            cmd = line.strip().lower()
            if cmd in ("play", "p"):
                await self.cmd_play()
            elif cmd in ("prepare", "load"):
                await self.cmd_prepare(None, None)
            elif cmd in ("stop", "s"):
                await self.cmd_stop()
            elif cmd in ("black", "blackout"):
                await self.cmd_blackout(True)
            elif cmd in ("unblack",):
                await self.cmd_blackout(False)
            elif cmd in ("hibernate", "sleep"):
                print(await self.cmd_hibernate())
            elif cmd in ("wake",):
                print(await self.cmd_wake())
            elif cmd in ("nodes", "n"):
                print(f"[ctl] nodes up: {sorted(self.nodes.keys())}")
            elif cmd in ("quit", "q", "exit"):
                break

    async def run(self, autoplay: bool):
        self._loop = asyncio.get_event_loop()
        _start_http(self)
        async with websockets.serve(self.handler, "0.0.0.0", self.ws_port,
                                    max_size=4 * 1024 * 1024):
            print(f"[ctl] WebSocket broker on ws/{self.ws_port}, "
                  f"web UI on http://0.0.0.0:{self.http_port}")
            tasks = [asyncio.create_task(self.cli())]
            if autoplay:
                tasks.append(asyncio.create_task(self.autoplay()))
            await asyncio.gather(*tasks)


def _read_line() -> Optional[str]:
    try:
        return input()
    except (EOFError, KeyboardInterrupt):
        return None


class _ApiHandler(SimpleHTTPRequestHandler):
    """Serves the calibration site AND the Node-RED power API on the same port:
        POST /api/hibernate   put the room to sleep (real power-down)
        POST /api/wake        wake it back up
        GET  /api/power       current power state
    Auth: if IMMERSIVE_API_TOKEN is set, require it as ?token= or X-Auth-Token.
    Everything else falls through to static file serving from web/."""
    controller: "Controller" = None  # set by _start_http

    def _authed(self) -> bool:
        tok = self.controller.api_token
        if not tok:
            return True
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        return self.headers.get("X-Auth-Token") == tok or q.get("token", [""])[0] == tok

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _call(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self.controller._loop)
        return fut.result(timeout=30)

    def _param(self, key: str):
        """Read a param from the query string or a JSON body."""
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        if key in q:
            return q[key][0]
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n:
            try:
                return json.loads(self.rfile.read(n)).get(key)
            except Exception:
                return None
        return None

    def do_POST(self):
        if not self.path.startswith("/api/"):
            self.send_error(404); return
        path = self.path.split("?", 1)[0]
        c = self.controller
        # Upload is the one POST the browser UI makes; it can't carry the token,
        # and the WebSocket control plane on the trusted net is already open.
        if path == "/api/upload":
            self._upload(); return
        if not self._authed():
            self._json(401, {"error": "unauthorized"}); return
        if path == "/api/hibernate":
            self._json(200, self._call(c.cmd_hibernate()))
        elif path == "/api/wake":
            self._json(200, self._call(c.cmd_wake()))
        elif path == "/api/play":
            media = self._param("media")           # which video to play
            self._json(200, self._call(c.cmd_play_media(media)))
        elif path == "/api/stop":
            self._call(c.cmd_stop()); self._json(200, {"playing": False})
        else:
            self._json(404, {"error": "unknown endpoint"})

    def _upload(self):
        """POST /api/upload?name=<file> with the video as the raw body."""
        import os, re
        from urllib.parse import urlparse, parse_qs, unquote
        c = self.controller
        name = os.path.basename(unquote(parse_qs(urlparse(self.path).query)
                                        .get("name", [""])[0]))
        if not re.match(r'^[\w.\- ]+\.(mp4|mov|mkv|h264)$', name, re.I):
            self._json(400, {"error": "filename must be a video (mp4/mov/mkv/h264)"})
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            self._json(400, {"error": "empty upload"}); return
        dst = c.media_dir / name
        with open(dst, "wb") as f:
            left = n
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                f.write(chunk); left -= len(chunk)
        print(f"[ctl] uploaded {name} ({n} bytes)")
        asyncio.run_coroutine_threadsafe(
            c._broadcast_web({"type": "playlist", "playlist": c.playlist()}), c._loop)
        self._json(200, {"uploaded": name, "playlist": c.playlist()})

    def _serve_media(self, name: str):
        import os, shutil
        from urllib.parse import unquote
        f = self.controller.media_dir / os.path.basename(unquote(name))
        if not f.exists():
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(f.stat().st_size))
        self.end_headers()
        with open(f, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/media/"):
            self._serve_media(path[len("/media/"):]); return
        if path in ("/api/power", "/api/playlist", "/api/videos", "/api/show"):
            if not self._authed():
                self._json(401, {"error": "unauthorized"}); return
            if path == "/api/power":
                self._json(200, self.controller.power_status())
            elif path in ("/api/playlist", "/api/videos"):
                pl = self.controller.playlist()
                self._json(200, {"videos": pl["videos"], "count": len(pl["videos"]),
                                 "current": pl["current"], "playing": pl["playing"],
                                 "media_dir": pl["media_dir"]})
            else:
                self._json(200, self.controller.show)
            return
        super().do_GET()

    def log_message(self, *a):
        pass  # quiet; the controller prints its own lines


def _start_http(controller: "Controller"):
    if not WEB_DIR.exists():
        WEB_DIR.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(_ApiHandler, directory=str(WEB_DIR))
    # bind the controller onto the handler class the partial wraps
    _ApiHandler.controller = controller
    httpd = ThreadingHTTPServer(("0.0.0.0", controller.http_port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws-port", type=int, default=8765)
    ap.add_argument("--clock-port", type=int, default=8555)
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--room-model", type=Path, default=None)
    ap.add_argument("--autoplay", action="store_true",
                    help="wait for the model's nodes, then prepare+play")
    ap.add_argument("--with-clock", action="store_true",
                    help="also run the GstNetTimeProvider in-process")
    args = ap.parse_args(argv)

    room = RoomModel(args.room_model) if args.room_model else RoomModel()

    if args.with_clock:
        _start_clock_thread(args.clock_port)

    ctl = Controller(room, args.ws_port, args.clock_port, args.http_port)
    try:
        asyncio.run(ctl.run(args.autoplay))
    except KeyboardInterrupt:
        pass
    return 0


def _start_clock_thread(port: int):
    def run():
        try:
            from clock_master import ClockMaster
            ClockMaster(port).run()
        except Exception as e:  # pragma: no cover - hardware/runtime dependent
            print(f"[ctl] clock master unavailable: {e}")
    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
