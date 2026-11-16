"""Room control and protocol-v2 numbered-frame presentation barriers.

The master verifies identical media, waits for every decoder and GPU, then
commits frames against a shared GstNet clock with a bounded native-buffer
horizon. It measures actual DRM presentation acknowledgements and stops sessions
on stale status, disconnects or missed deadlines. The HTTP server hosts the
admin UI and same-origin WebSocket bridge for browsers.
"""

from __future__ import annotations

import argparse
import copy
import asyncio
import functools
import concurrent.futures
import json
import os
import sys
import threading
import time
import uuid
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.frame_sync import PROTOCOL_VERSION, FrameCoordinator, VideoSpec
from shared.service_health import EventLog
from shared.http_server import make_http_server
from shared.calibration import validate_solution, validate_field
from shared.media_package import load_plan, validate_tile_coverage

import powerctl
from room_devices import RoomDevices
from start_operation import managed_start
from phone_scan import PhoneScan
import nodeadmin

try:
    import websockets
    ConnClosed = websockets.ConnectionClosed
except Exception:  # importable on a dev box without the runtime dep
    websockets = None
    ConnClosed = Exception

from roommodel import RoomModel

WEB_DIR = Path(__file__).with_name("web")
LEAD_NS = 100_000_000  # presentation horizon, after every member is ready


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


def _read_version() -> str:
    try:
        return open("/etc/immersive-version").read().strip()
    except Exception:
        return "dev"


_master_clock_read = None


def now_ns() -> int:
    """Read the master clock without repeating GI setup on every frame event."""
    global _master_clock_read
    if _master_clock_read is None:
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            if not Gst.is_initialized():
                Gst.init(None)
            # Keep the same GstSystemClock as the network time provider.
            _master_clock_read = Gst.SystemClock.obtain().get_time
        except ImportError:
            _master_clock_read = time.monotonic_ns
    return _master_clock_read()


from shared.timing_trace import TimingTrace


class Controller:
    def __init__(self, room: RoomModel, ws_port: int, clock_port: int,
                 http_port: int, media_dir: Optional[Path] = None):
        self.trace = TimingTrace()
        self.room = room
        self.ws_port = ws_port
        self.clock_port = clock_port
        self.http_port = http_port
        self.media_dir = Path(media_dir) if media_dir else \
            Path(__file__).resolve().parent / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        ensure_test_video(self.media_dir)

        self.nodes: dict[str, websockets.WebSocketServerProtocol] = {}
        self.node_info: dict[str, dict] = {}   # node -> {mac, serial} from hello
        self.discovered: dict[str, str] = {}   # node -> addr, found via mDNS
        self.webs: set[websockets.WebSocketServerProtocol] = set()
        self._web_out = {}
        self.heartbeats: dict[str, dict] = {}
        self.show: dict = {
            "trial": os.environ.get("IMMERSIVE_TRIAL") == "1",
            "show": room.model.get("show", ""),
            "media": room.model.get("media", ""),
            "playing": False,
            "base_time_ns": None,
            "loop_epoch_ns": None,
        }
        # Hibernation / Node-RED power control.
        self.power = powerctl.make(room.model.get("power"))
        self.power_cfg = room.model.get("power", {}) or {}
        self.devices = RoomDevices(room.model.get("room_devices"))
        self._room_operation = asyncio.Lock()
        self.power_state = "awake"          # awake | hibernating
        self._was_playing = False
        self.api_token = os.environ.get("IMMERSIVE_API_TOKEN", "")
        import socket
        self.admin_password = nodeadmin.admin_password(socket.gethostname())
        self._started = __import__("time").monotonic()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._frame_task = None
        self._start_task = None
        self._session = None
        self._members = []
        self._media_plan = None
        self._ready_nodes = {}
        self._frame_status = {}
        self._ready_event = asyncio.Event()
        self._coordinator = None
        self._frame_receipts = {}
        self.events = EventLog(os.environ.get("IMMERSIVE_STATE_DIR"))
        self.phone_scan = PhoneScan(self)

    def admin_health(self) -> dict:
        import socket, time
        up = 0.0
        try:
            up = float(open("/proc/uptime").read().split()[0])
        except Exception:
            pass
        return {
            "node": socket.gethostname(), "role": "control",
            "version": _read_version(), "uptime_s": int(up),
            "nodes_up": len(self.nodes), "playing": self.show.get("playing"),
            "media": self.show.get("media"), "power_state": self.power_state,
            "room_devices": self.devices.snapshot,
            "phone_scan_active": self.phone_scan.active,
        }

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
            info = {"mac": (hello.get("mac") or "").lower(), "serial": hello.get("serial"),
                    "protocol": hello.get("protocol", 1),
                    "capabilities": hello.get("capabilities", [])}
            await self._serve_node(ws, hello.get("hello", "unknown"), info)
        else:
            await self._serve_web(ws)

    async def _serve_node(self, ws, node: str, info: dict):
        self.nodes[node] = ws
        self.node_info[node] = info
        self.events.record("connected", node, protocol=info.get("protocol", 1))
        await self._broadcast_web({"type": "nodes", "nodes_up": list(self.nodes)})
        enrolled = self.room.entry_for(node) is not None
        print(f"[ctl] node {node} connected "
              f"({'enrolled' if enrolled else 'PENDING enrollment'}, mac={info.get('mac')})")
        if not enrolled:
            # connected but not in the room-model — surface for assignment
            await self._broadcast_web({"type": "pending", "pending": self.pending()})
            await self._serve_node_loop(ws, node)
            return
        # On connect, push this node its own room-model entry so it warps
        # correctly immediately, before any show starts.
        await self._push_entry(node)
        # If a show is already playing, arm the late joiner onto the same base
        # time and loop epoch — this is also the node-swap path: a replaced Pi
        # boots, connects, and falls straight into the running ring.
        if self._session and node in self._members:
            await self._fail_session(f"{node} reconnected; restart the show to prepare the full fleet")
        await self._serve_node_loop(ws, node)

    async def _serve_node_loop(self, ws, node: str):
        try:
            async for raw in ws:
                await self._on_node_msg(node, raw)
        except ConnClosed:
            pass
        finally:
            was_pending = self.room.entry_for(node) is None
            if self.nodes.get(node) is ws:
                await self.phone_scan.cancel(f"{node} disconnected during phone scanning")
                self.nodes.pop(node, None)
                self.node_info.pop(node, None)
                self.heartbeats.pop(node, None)
                self.events.record("disconnected", node)
                if self._session and node in self._members:
                    await self._fail_session(f"{node} disconnected")
                await self._broadcast_web({"type": "nodes", "nodes_up": list(self.nodes)})
            print(f"[ctl] node {node} disconnected ({len(self.nodes)} up)")
            if was_pending:
                await self._broadcast_web({"type": "pending", "pending": self.pending()})

    def pending(self) -> dict:
        """Connected nodes that are not in the room-model — awaiting assignment."""
        return {n: self.node_info.get(n, {}) for n in self.nodes
                if self.room.entry_for(n) is None}

    # ---- mDNS auto-discovery (control browses for nodes on the network) ---
    def _mdns_browse(self) -> dict:
        """{node_id: address} for _immersive._tcp services seen via avahi.
        Runs on the Pi (avahi-browse); silently empty elsewhere."""
        import subprocess
        found = {}
        try:
            r = subprocess.run(["avahi-browse", "-rpt", "_immersive._tcp"],
                               capture_output=True, text=True, timeout=8)
        except Exception:
            return found
        for line in r.stdout.splitlines():
            if not line.startswith("="):
                continue
            f = line.split(";")
            if len(f) < 10:
                continue
            host, txt = f[6], f[9]
            node = None
            for kv in txt.replace('" "', "\x00").strip('"').split("\x00"):
                if kv.startswith("node="):
                    node = kv[5:]
            if node:
                found[node] = host
        return found

    async def _mdns_browse_loop(self):
        while True:
            await asyncio.sleep(15)
            try:
                found = await self._loop.run_in_executor(None, self._mdns_browse)
            except Exception:
                continue
            disc = {n: a for n, a in found.items()
                    if self.room.entry_for(n) is None and n not in self.nodes}
            if disc != getattr(self, "discovered", {}):
                self.discovered = disc
                await self._broadcast_web({"type": "discovered", "discovered": disc})

    async def _serve_web(self, ws):
        self.webs.add(ws)
        outbound = asyncio.Queue(maxsize=64)
        self._web_out[ws] = outbound
        async def send_updates():
            try:
                while True:
                    await asyncio.wait_for(ws.send(await outbound.get()), timeout=1)
            except (asyncio.TimeoutError, ConnClosed):
                await ws.close()
        sender = asyncio.create_task(send_updates())
        # Send a snapshot so a freshly loaded tablet has the full model + state.
        await self._send(ws, {"type": "snapshot",
                              "room": self.room.model,
                              "show": self.show,
                              "heartbeats": self.heartbeats,
                              "nodes_up": list(self.nodes.keys()),
                              "playlist": self.playlist(),
                              "power_state": self.power_state,
                              "room_devices": self.devices.snapshot,
                              "pending": self.pending(),
                              "discovered": self.discovered})
        try:
            async for raw in ws:
                try:
                    await self._on_web_msg(raw, origin=ws)
                except (ValueError, KeyError, TypeError) as exc:
                    try:
                        request = json.loads(raw)
                    except ValueError:
                        request = {}
                    request = request if isinstance(request, dict) else {}
                    await self._send(ws, {"type":"error", "message":str(exc),
                        "cmd":request.get("cmd"), "node":request.get("node"),
                        "request_id":request.get("request_id")})
        except ConnClosed:
            pass
        finally:
            await self.phone_scan.cancel("phone scan connection closed", owner=ws)
            self.webs.discard(ws)
            self._web_out.pop(ws, None)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    # ---- inbound from nodes ---------------------------------------------
    async def _on_node_msg(self, node: str, raw):
        msg = json.loads(raw)
        if msg.get("type") in ("capture_displayed", "capture_error"):
            self.phone_scan.receipt(node, msg)
            return
        if msg.get("type") in ("ready", "frame_status", "frame_staged", "frame_presented", "node_error"):
            if msg.get("session") != self._session or node not in self._members:
                return
            if msg["type"] in ("frame_staged", "frame_presented"):
                self.trace.record("receipt", node=node, clock_ns=now_ns(), packet=msg)
            if msg["type"] == "frame_staged":
                if self._coordinator:
                    try:
                        self._coordinator.staged_frame(node, int(msg["sequence"]))
                    except ValueError as exc:
                        await self._fail_session(str(exc))
            elif msg["type"] == "frame_presented":
                master = self._coordinator
                if master is None:
                    return
                sequence = int(msg["sequence"])
                period = master.spec.offset_ns(1)
                if sequence < 0 or sequence >= master.sequence or msg["frame"] != master.spec.frame_index(sequence):
                    await self._fail_session(f"{node} acknowledged an invalid video frame")
                    return
                if sequence < master.presented[node]:
                    await self._fail_session(f"{node} acknowledged an older frame")
                    return
                # Release the native-buffer staging horizon immediately. Waiting
                # for the periodic status packet adds up to 20ms to every frame.
                master.presented[node] = sequence
                master.last_seen[node] = time.monotonic()
                if abs(msg["presentation_error_ns"]) > period:
                    await self._fail_session(f"{node} missed frame {sequence}'s deadline by "
                                             f"{msg['presentation_error_ns'] / 1e6:.1f} ms")
                    return
                receipt = self._frame_receipts.setdefault(sequence, {})
                receipt[node] = msg["presented_at_ns"]
                if len(receipt) == len(self._members):
                    skew = max(receipt.values()) - min(receipt.values())
                    self.show["presentation_skew_ns"] = skew
                    self.show["max_presentation_skew_ns"] = max(self.show.get("max_presentation_skew_ns", 0), skew)
                    if skew > period:
                        await self._fail_session(f"frame {sequence} presentation skew exceeded one video frame")
                for old in list(self._frame_receipts):
                    if old < sequence - 120:
                        del self._frame_receipts[old]
            elif msg["type"] == "ready":
                self._ready_nodes[node] = msg["spec"]
                self._ready_event.set()
                await self._broadcast_web({"type": "prepare_progress", "ready": sorted(self._ready_nodes),
                                           "waiting": sorted(set(self._members) - self._ready_nodes.keys())})
            elif msg["type"] == "node_error" or msg.get("error"):
                await self._fail_session(f"{node}: {msg.get('error', 'playback failed')}")
                self._ready_event.set()
            else:
                self._frame_status[node] = {**msg, "received": time.monotonic()}
                if self._coordinator:
                    try:
                        # The status snapshot and immediate flip receipt originate on
                        # different node threads. A delayed snapshot must not
                        # roll a newer, validated presentation acknowledgement back.
                        presented = max(msg["presented"], self._coordinator.presented[node])
                        self._coordinator.report(node, msg["buffered_first"], msg["buffered_last"], presented)
                    except ValueError as exc:
                        await self._fail_session(str(exc))
            return
        if "preview" in msg:
            # Compatibility with older nodes: discard images, retain telemetry.
            return
        if "node" in msg and "clock_offset_ns" in msg:
            self.phone_scan.display_status(node, msg.get("capture_display"))
            previous = self.heartbeats.get(node, {})
            hardware = msg.get("hardware", {})
            old_hardware = previous.get("hardware", {})
            if hardware.get("boot_id") and old_hardware.get("boot_id") and hardware["boot_id"] != old_hardware["boot_id"]:
                self.events.record("rebooted", node, boot_id=hardware["boot_id"])
            if hardware.get("faults") != old_hardware.get("faults"):
                self.events.record("hardware_faults", node, faults=hardware.get("faults", []))
            for field in ("playback_error", "display_error"):
                if msg.get(field) != previous.get(field):
                    self.events.record(field, node, error=msg.get(field))
            self.heartbeats[node] = msg
            off_ms = msg["clock_offset_ns"] / 1e6
            print(f"[hb] {node:6} off={off_ms:+8.3f}ms "
                  f"pos={msg.get('media_pos_ns', 0)/1e9:7.3f}s "
                  f"dec={'ok' if msg.get('decoder_ok') else 'BAD'} "
                  f"fb={'ok' if msg.get('fb_ok') else 'BAD'} "
                  f"{msg.get('temp_c', 0):.1f}C")
            await self._broadcast_web({"type": "heartbeat", "hb": msg})

    # ---- inbound from web (calibration site) ----------------------------
    async def _on_web_msg(self, raw, origin=None):
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            raise ValueError("WebSocket command must be an object")
        cmd = msg.get("cmd")
        if isinstance(cmd, str) and cmd.startswith("capture_"):
            await self.phone_scan.process(origin, msg)
            return
        if cmd == "scan" and msg.get("on", True):
            raise ValueError("Legacy autocalib scanning is retired and must not be used")
        if cmd not in ("list_media", "autocalibrate"):
            await self.phone_scan.cancel(f"room command {cmd} interrupted phone scanning")
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
            if msg.get("on", True) and self.show["playing"]:
                await self.cmd_stop()
            await self._send_node(msg["node"], msg)
        elif cmd == "identify":
            if self.show["playing"]:
                await self.cmd_stop()
            await self._send_node(msg["node"], msg)
        elif cmd == "prepare":
            await self.cmd_prepare(msg.get("show"), msg.get("media"))
        elif cmd == "play":
            await self.cmd_play()
        elif cmd == "stop":
            await self.cmd_stop()
        elif cmd == "blackout":
            if self.show["playing"]:
                await self.cmd_stop()
            if msg.get("node"):
                await self._send_node(msg["node"], {"cmd": "blackout", "on": msg.get("on", True)})
            else:
                await self.cmd_blackout(msg.get("on", True))
        elif cmd == "scan":
            if msg.get("on", True):
                raise ValueError("Legacy autocalib scanning is retired and must not be used")
            # Permit clearing an old pattern during experimental tool cleanup.
            await self._send_node(msg["node"], {
                "cmd": "scan_show", "axis": msg.get("axis"), "bit": msg.get("bit"),
                "inverted": msg.get("inverted", False), "on": msg.get("on", True)})
        elif cmd == "play_media":
            if self._start_task:
                self._start_task.cancel()
            self._start_task = asyncio.create_task(self._play_request(msg.get("media")))
        elif cmd == "list_media":
            await self._broadcast_web({"type": "playlist", "playlist": self.playlist()})
        elif cmd == "add_node":
            self.room.ensure_node(msg["node"], msg.get("projector", ""))
            if msg.get("mac") or msg.get("addr"):
                self.room.set_node_net(msg["node"], mac=msg.get("mac"),
                                       addr=msg.get("addr"))
            self.room.save(f"add node {msg['node']}")
            await self._push_entry(msg["node"])
            await self._broadcast_room()
        elif cmd == "remove_node":
            if self.room.remove_node(msg["node"]):
                self.room.save(f"remove node {msg['node']}")
                await self._broadcast_room()
        elif cmd == "set_node_projector":
            self.room.set_node_net(msg["node"], projector=msg.get("projector"))
            self.room.save(f"set projector {msg['node']}")
            await self._broadcast_room()
        elif cmd == "enroll_node":
            await self.cmd_enroll(msg)
        elif cmd == "autocalibrate":
            asyncio.create_task(self.cmd_autocalibrate(msg))
        elif cmd == "room_device":
            try:
                if msg.get("target", "").startswith(("beamer/", "panorama", "praesi")):
                    await self.cmd_stop()
                async with self._room_operation:
                    await self.devices.command(msg.get("target", ""), msg.get("action", ""))
                    await self.devices.refresh()
                    await self._broadcast_web({"type": "room_devices", "devices": self.devices.snapshot})
            except Exception as exc:
                await self._broadcast_web({"type": "error", "message": str(exc)})
        elif cmd == "hibernate":
            try:
                await self.cmd_hibernate()
            except Exception as exc:
                await self._broadcast_web({"type": "error", "message": str(exc)})
        elif cmd == "wake":
            if self._start_task:
                self._start_task.cancel()
            self._start_task = asyncio.create_task(self._wake_request())
        elif cmd == "save":
            self.room.save(msg.get("message", "calibration save"))
            await self._broadcast_web({"type": "saved",
                                       "version": self.room.model["version"]})

    async def _edit(self, cmd: str, msg: dict):
        field = {"set_mesh": "mesh", "set_blend": "blend",
                 "set_color": "color", "set_source_region": "source_region"}[cmd]
        node = msg["node"]
        value = msg[field]
        validate_field(field, value)
        candidate = self.room.entry_for(node)
        if candidate is None:
            raise ValueError('unknown calibration node: ' + str(node))
        candidate[field] = copy.deepcopy(value)
        if self._session and self._media_plan and field in ('mesh', 'source_region'):
            validate_tile_coverage(candidate, self._media_plan['nodes'][node]['region'])
        self.room.update_field(node, field, value)
        # Live edits update the projector shader.
        await self._send_node(node, {"cmd": cmd, "node": node, field: value})
        await self._broadcast_web({"type": "entry", "entry": self.room.entry_for(node)})

    async def _mesh_delta(self, msg: dict):
        node = msg["node"]
        entry = self.room.apply_mesh_delta(node, msg["index"], msg["x"], msg["y"])
        await self._send_node(node, {"cmd": "set_mesh", "node": node, "mesh": entry["mesh"]})
        await self._broadcast_web({"type": "entry", "entry": entry})

    # ---- show control ----------------------------------------------------
    async def _cancel_frame_task(self):
        task, self._frame_task = self._frame_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # Awaiting the cancelled frame loop also receives cancellation
                # directed at this caller. Do not swallow the latter: node
                # receivers and shutdown gathers must still be able to exit.
                if asyncio.current_task().cancelling():
                    raise
        self._coordinator = None

    async def _fail_session(self, error):
        self.events.record("session_failed", error=error, session=self._session)
        await self._cancel_frame_task()
        self.show.update(playing=False, state="error", error=error)
        self._session = None
        self._ready_event.set()
        # Hold the last displayed frame, cancel outstanding commits everywhere.
        await self._broadcast_nodes({"cmd": "stop"})
        await self._broadcast_web({"type": "show", "show": self.show})
        await self._broadcast_web({"type": "error", "message": error})
        print(f"[ctl] frame session failed: {error}")

    async def _play_request(self, media):
        try:
            await self.cmd_play_media(media)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail_session(str(exc))

    async def cmd_prepare(self, show: Optional[str], media: Optional[str]):
        if getattr(self, "phone_scan", None):
            await self.phone_scan.cancel("video preparation interrupted phone scanning")
        await self._cancel_frame_task()
        await self._broadcast_nodes({"cmd": "stop"})
        self._session = None
        self.show.update(playing=False, state="preparing", error=None,
                         source_width=None, source_height=None, media_mode=None)
        self._members = self.room.nodes()
        if not self._members:
            raise RuntimeError("no render nodes are enrolled")
        missing = sorted(set(self._members) - self.nodes.keys())
        if missing:
            raise RuntimeError("cannot start a frame-locked show; disconnected: " + ", ".join(missing))
        legacy = [n for n in self._members if self.node_info[n].get("protocol", 1) != PROTOCOL_VERSION]
        if legacy:
            raise RuntimeError("upgrade nodes to frame protocol v2: " + ", ".join(legacy))
        media = media or self.room.model.get("media", "pan.mp4")
        if Path(media).name != media:
            raise ValueError("media must be a plain filename")
        plan = await asyncio.to_thread(load_plan, self.media_dir, media, self._members)
        for node in self._members:
            validate_tile_coverage(self.room.entry_for(node), plan['nodes'][node]['region'])
        if plan["spec"]:
            unsupported = [n for n in self._members if "per_node_video_v1" not in self.node_info[n].get("capabilities", [])]
            if unsupported:
                raise RuntimeError("upgrade nodes for per-projector video: " + ", ".join(unsupported))
        self._media_plan = plan
        sha = plan["sha256"]
        self._session = uuid.uuid4().hex
        self.events.record("preparing", session=self._session, media=media, members=self._members)
        self._ready_nodes = {}
        self._frame_status = {}
        self._frame_receipts = {}
        self._ready_event.clear()
        self.show.update(show=show or self.room.model.get("show", "show"), media=media,
                         protocol=PROTOCOL_VERSION, session=self._session, sha256=sha,
                         base_time_ns=None, loop_epoch_ns=None, holds=0,
                         committed_sequence=-1, presentation_skew_ns=None,
                         max_presentation_skew_ns=0)
        await self._broadcast_web({"type": "show", "show": self.show})
        for node in self._members:
            asset = plan["nodes"][node]
            await self._send_node(node, {"cmd": "prepare", "protocol": PROTOCOL_VERSION,
                "session": self._session, "slice": asset["file"], "sha256": asset["sha256"],
                "video_region": asset["region"], "clock_port": self.clock_port})

    async def cmd_play(self):
        if getattr(self, "phone_scan", None):
            await self.phone_scan.cancel("video playback interrupted phone scanning")
        if not self._session:
            await self.cmd_prepare(None, self.show.get("media"))
        session = self._session
        deadline = asyncio.get_running_loop().time() + 120
        while set(self._ready_nodes) != set(self._members):
            if session != self._session:
                raise RuntimeError(self.show.get("error") or "preparation was cancelled")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError("prepare timed out; waiting for " + ", ".join(set(self._members) - self._ready_nodes.keys()))
            self._ready_event.clear()
            try:
                await asyncio.wait_for(self._ready_event.wait(), min(remaining, 1))
            except asyncio.TimeoutError:
                pass
        if session != self._session:
            raise RuntimeError("preparation was cancelled")
        specs = [VideoSpec(**{k: self._ready_nodes[n][k] for k in ("fps_num", "fps_den", "frames", "sha256")})
                 for n in self._members]
        timing = lambda spec: (spec.fps_num, spec.fps_den, spec.frames)
        for node, spec in zip(self._members, specs):
            asset = self._media_plan["nodes"][node]
            if spec.sha256 != asset["sha256"] or timing(spec) != timing(specs[0]):
                raise RuntimeError(f"{node} disagrees on video content, frame rate or frame count")
            declared = self._media_plan["spec"]
            if declared and timing(spec) != (declared["fps_num"], declared["fps_den"], declared["frames"]):
                raise RuntimeError(f"{node} video timing disagrees with its tile manifest")
            if declared and any(self._ready_nodes[node][k] != asset[k] for k in ("width", "height")):
                raise RuntimeError(f"{node} video resolution disagrees with its tile manifest")
        show_spec = VideoSpec(specs[0].fps_num, specs[0].fps_den, specs[0].frames, self.show["sha256"])
        self._coordinator = FrameCoordinator(self._members, show_spec, lead_ns=LEAD_NS, max_ahead=3, require_staged=True)
        for node, status in self._frame_status.items():
            self._coordinator.report(node, status["buffered_first"], status["buffered_last"], status["presented"], status["received"])
        self.show.update(media_mode="tiles" if self._media_plan["spec"] else "panorama",
                         source_width=self._ready_nodes[self._members[0]]["width"],
                         source_height=self._ready_nodes[self._members[0]]["height"],
                         playing=True, state="playing", fps_num=specs[0].fps_num,
                         fps_den=specs[0].fps_den, frames=specs[0].frames)
        self._frame_task = asyncio.create_task(self._frame_loop(session))
        await self._broadcast_web({"type": "show", "show": self.show})

    async def _frame_loop(self, session):
        started = time.monotonic()
        stalled_since = None
        last_progress = 0
        try:
            while session == self._session:
                master = self._coordinator
                missing = master.missing()
                if missing and time.monotonic() - started > 2:
                    raise RuntimeError("frame status lost from " + ", ".join(missing))
                reservation = master.reserve() if not missing else None
                if reservation:
                    self.trace.record("reserve", **reservation, clock_ns=now_ns(), ready=dict(master.ready), presented=dict(master.presented))
                if reservation:
                    for node in self._members:
                        await self._send_node(node, {"cmd": "stage_frame", "session": session, **reservation})
                old_holds = master.holds
                commit = master.commit(now_ns()) if not missing else None
                if commit:
                    self.trace.record("commit", **commit, clock_ns=now_ns(), holds=master.holds, held=master.holds != old_holds, ready=dict(master.ready), staged=dict(master.staged), presented=dict(master.presented))
                if commit:
                    if self.show["base_time_ns"] is None:
                        self.show["base_time_ns"] = commit["present_at_ns"]
                    for node in self._members:
                        await self._send_node(node, {"cmd": "commit_frame", "session": session, **commit})
                    self.show["committed_sequence"] = commit["sequence"]
                    self.show["holds"] = master.holds
                    stalled_since = None
                else:
                    # The bounded horizon normally waits for vblank acknowledgements.
                    # Lack of any progress for 2s is an explicit fleet failure.
                    stalled_since = stalled_since or time.monotonic()
                    if time.monotonic() - stalled_since > 2:
                        raise RuntimeError("frame readiness barrier stalled")
                if time.monotonic() - last_progress > .5:
                    await self._broadcast_web({"type": "frame_status", "session": session,
                        "committed": master.sequence - 1, "presented": master.presented,
                        "holds": master.holds,
                        "presentation_skew_ns": self.show.get("presentation_skew_ns"),
                        "max_presentation_skew_ns": self.show.get("max_presentation_skew_ns")})
                    last_progress = time.monotonic()
                await asyncio.sleep(.002)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail_session(str(exc))

    @managed_start
    async def cmd_play_media(self, media: str) -> dict:
        await self.cmd_stop()
        async with self._room_operation:
            if self.devices.base:
                self.show.update(state="waiting_projectors", playing=False)
                await self._broadcast_web({"type": "show", "show": self.show})
                await self.devices.ready(self._broadcast_web)
                await self._wake_nodes()
            await self.cmd_prepare(self.show.get("show"), media)
            await self.cmd_play()
        return {"playing": True, "media": self.show["media"], "nodes": self._members}

    async def cmd_autocalibrate(self, opts: dict):
        """Legacy scanner is retired; camera calibration needs a new pipeline."""
        await self._broadcast_web({"type":"autocalib", "stage":"error",
            "msg":"Legacy autocalib is retired and must not be used. Seamcal remains experimental; a validated phone-scan workflow is not available yet."})

    def playlist(self) -> dict:
        """Playable shows; complete tile packages own their constituent clips."""
        vids = []
        owners = {}
        if self.media_dir.exists():
            vids = sorted(p.name for p in self.media_dir.iterdir()
                          if p.is_file() and (p.suffix.lower() in (".mp4", ".mov", ".mkv", ".h264") or p.name.endswith(".immersive.json")))
            for name in vids:
                if not name.endswith('.immersive.json'):
                    continue
                try:
                    package = json.loads((self.media_dir/name).read_text())
                    members = package.get('nodes')
                    if not isinstance(members, dict) or not members:
                        continue
                    plan = load_plan(self.media_dir, name, list(members))
                except (OSError, ValueError, TypeError, AttributeError):
                    continue  # A damaged/incomplete manifest cannot hide videos.
                for asset in plan['nodes'].values():
                    owners.setdefault(asset['file'], name)
            vids = [name for name in vids if name not in owners]
        current = self.show.get("media")
        return {"media_dir": str(self.media_dir), "videos": vids,
                "current": owners.get(current, current), "playing": self.show.get("playing")}

    async def cmd_stop(self):
        if getattr(self, "phone_scan", None):
            await self.phone_scan.cancel("playback stop interrupted phone scanning")
        if self._start_task and self._start_task is not asyncio.current_task():
            self._start_task.cancel()
        await self._cancel_frame_task()
        self._session = None
        self._ready_event.set()
        self.show.update(playing=False, state="stopped")
        await self._broadcast_nodes({"cmd": "stop"})
        await self._broadcast_web({"type": "show", "show": self.show})
        print("[ctl] stop")

    async def cmd_blackout(self, on: bool):
        if getattr(self, "phone_scan", None):
            await self.phone_scan.cancel("blackout command interrupted phone scanning")
        await self._broadcast_nodes({"cmd": "blackout", "on": on})
        print(f"[ctl] blackout {'on' if on else 'off'}")

    # ---- hibernation (driven by Node-RED via the REST API) ---------------
    async def cmd_hibernate(self):
        await self.cmd_stop()
        async with self._room_operation:
            if self.devices.base:
                await self.devices.command("panorama", "off")
                await self.devices.command("praesi", "off")
            return await self._hibernate_nodes()

    async def _hibernate_nodes(self):
        """Put the room to sleep with a real power-down. Render nodes blank and
        clean-poweroff themselves; then the configured power backend cuts their
        power. The control node stays up to receive the wake call."""
        if self.power_state == "hibernating":
            return {"state": "hibernating", "note": "already hibernating"}
        self._was_playing = self.show.get("playing", False)
        await self.cmd_stop()
        self.power_state = "hibernating"
        self.show["playing"] = False
        if self.power.can_wake:
            # real power-down: nodes clean-poweroff, then cut switched power
            await self._broadcast_nodes({"cmd": "sleep", "poweroff": True})
            grace = float(self.power_cfg.get("poweroff_grace_s", 8))
            asyncio.create_task(self._cut_power_after(grace))
            mode = "poweroff"
        else:
            # no switched power to cold-boot the nodes (constant-power supply):
            # soft standby — nodes stay on the network and blank the projector
            # output, woken later by a plain 'wake' message. No WoL/wiring needed.
            await self._broadcast_nodes({"cmd": "sleep", "poweroff": False,
                                         "standby": True})
            mode = "standby"
        await self._broadcast_web({"type": "power", "state": "hibernating",
                                   "mode": mode})
        print(f"[ctl] HIBERNATE ({mode}): {len(self.nodes)} nodes, "
              f"backend={self.power_cfg.get('backend', 'none')}")
        return {"state": "hibernating", "mode": mode,
                "nodes": list(self.nodes.keys()),
                "backend": self.power_cfg.get("backend", "none")}

    async def _cut_power_after(self, grace: float):
        await asyncio.sleep(grace)
        for node in self.room.nodes():
            await self.power.power_off(node)

    async def _wake_request(self):
        try:
            await self.cmd_wake()
        except Exception as exc:
            await self._broadcast_web({"type": "error", "message": str(exc)})

    @managed_start
    async def cmd_wake(self):
        async with self._room_operation:
            if self.devices.base:
                await self.devices.ready(self._broadcast_web)
            return await self._wake_nodes()

    async def _wake_nodes(self):
        """Wake the room: restore switched power so the nodes cold-boot, wait for
        them to rejoin, then resume the show if it was playing."""
        if self.power_state == "awake":
            return {"state": "awake", "note": "already awake"}
        if self.power.can_wake:
            # hardware wake: restore switched power, wait for the cold-boot rejoin
            for node in self.room.nodes():
                await self.power.power_on(node)
            self.power_state = "awake"
            await self._broadcast_web({"type": "power", "state": "waking"})
            wait_s = float(self.power_cfg.get("wake_wait_s", 45))
            asyncio.create_task(self._resume_after_wake(wait_s))
            print(f"[ctl] WAKE (power): restoring power, up to {wait_s:.0f}s")
            return {"state": "waking", "wake_wait_s": wait_s,
                    "resume_play": self._was_playing}
        # soft standby: the nodes never left the network — tell them to resume
        # and replay the show. Works with no switched power.
        await self._broadcast_nodes({"cmd": "wake"})
        self.power_state = "awake"
        await self._broadcast_web({"type": "power", "state": "awake", "mode": "standby"})
        print(f"[ctl] WAKE (standby): {len(self.nodes)} nodes resumed")
        return {"state": "awake", "mode": "standby",
                "resume_play": self._was_playing}

    async def _resume_after_wake(self, wait_s: float):
        want = set(self.room.nodes())
        deadline = wait_s
        while deadline > 0 and not want.issubset(self.nodes.keys()):
            await asyncio.sleep(1.0)
            deadline -= 1.0
        await self._broadcast_web({"type": "power", "state": "awake"})
        if self._was_playing:
            await self.cmd_play_media(self.show.get("media"))
        print(f"[ctl] awake: {len(self.nodes)}/{len(want)} nodes back"
              f"{' , show resumed' if self._was_playing else ''}")

    def power_status(self) -> dict:
        return {
            "state": self.power_state,
            "backend": self.power_cfg.get("backend", "none"),
            "can_wake": self.power.can_wake,
            "nodes_up": list(self.nodes.keys()),
            "was_playing": self._was_playing,
            "room_devices": self.devices.snapshot,
        }

    async def _device_status_loop(self):
        while True:
            await self.devices.refresh()
            await self._broadcast_web({"type": "room_devices", "devices": self.devices.snapshot})
            await asyncio.sleep(3)

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

    async def cmd_enroll(self, msg: dict):
        """Assign a pending (connected-but-unknown) node a real id and role. No
        IP is assigned — the node keeps its DHCP address and is reached by mDNS
        (<node>.local). The node adopts the identity and rejoins."""
        pending_id = msg["pending"]               # id the node is connected as
        assigned = msg["node"]
        role = msg.get("role", "render")
        info = self.node_info.get(pending_id, {})
        mac = info.get("mac")
        self.room.ensure_node(assigned, msg.get("projector", ""))
        if mac:
            self.room.set_node_net(assigned, mac=mac)   # for identification only
        self.room.save(f"enroll {assigned} (mac {mac}) as {role}")
        ws = self.nodes.get(pending_id)
        if ws:
            await self._send(ws, {"cmd": "adopt", "node": assigned, "role": role})
        print(f"[ctl] enrolled {pending_id} -> {assigned} (mac {mac}, {role})")
        await self._broadcast_room()
        await self._broadcast_web({"type": "pending", "pending": self.pending()})

    async def _broadcast_room(self):
        """Push the full model + playlist after a roster/network change so every
        open tablet re-renders the node list."""
        await self._broadcast_web({"type": "room", "room": self.room.model,
                                   "nodes_up": list(self.nodes.keys()),
                                   "playlist": self.playlist()})

    async def _broadcast_web(self, obj: dict):
        if not self.webs or obj.get("preview"):
            return
        data = json.dumps(obj)
        for ws in list(self.webs):
            try:
                self._web_out[ws].put_nowait(data)
            except asyncio.QueueFull:
                # A slow tablet must never stall the frame coordinator. It can
                # skip replaceable telemetry; a reconnect restores full state.
                if obj.get("preview") or obj.get("type") in ("heartbeat", "frame_status"):
                    continue
                asyncio.create_task(ws.close())
            except KeyError:
                continue

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
        # Keep transport CPU work off the frame barrier's timing budget.
        async with websockets.serve(self.handler, ["0.0.0.0", "::"], self.ws_port,
                                    compression=None, max_size=4 * 1024 * 1024):
            print(f"[ctl] WebSocket broker on ws/{self.ws_port}, "
                  f"web UI on http://0.0.0.0:{self.http_port}")
            tasks = [asyncio.create_task(self.cli()),
                     asyncio.create_task(self._mdns_browse_loop()),
                     asyncio.create_task(self._device_status_loop())]
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
        try:
            # Projector warm-up precedes the decoder's 120-second prepare gate.
            timeout = float(self.controller.devices.config.get("ready_timeout_s", 180)) + 150
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise TimeoutError("Room operation timed out and was cancelled") from None

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
        try:
            self._post()
        except concurrent.futures.CancelledError:
            self._json(409, {"error": "Room operation cancelled"})
        except TimeoutError as exc:
            self._json(504, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._json(409, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def _post(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/admin/api/"):
            path = path[len("/admin"):]
        if not path.startswith("/api/"):
            self.send_error(404); return
        c = self.controller
        # Upload is the one POST the browser UI makes; it can't carry the token,
        # and the WebSocket control plane on the trusted net is already open.
        if path == "/api/upload":
            self._upload(); return
        # admin routes use Basic auth (not the token), same as every node
        if path == "/api/config":
            if self._basic_ok():
                n = int(self.headers.get("Content-Length", 0) or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    self._json(400, {"error": "bad json"}); return
                self._json(200, {"saved": True,
                                 "config": nodeadmin.write_bootconfig(body.get("config", {}))})
            return
        if path == "/api/reboot":
            if self._basic_ok():
                self._json(200, {"rebooting": True})
                import subprocess
                subprocess.Popen(["systemctl", "reboot"])
            return
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
        import os, re, tempfile
        from urllib.parse import urlparse, parse_qs, unquote
        c = self.controller
        name = os.path.basename(unquote(parse_qs(urlparse(self.path).query)
                                        .get("name", [""])[0]))
        if not re.match(r'^[\w.\- ]+\.(mp4|mov|mkv|h264|immersive\.json)$', name, re.I):
            self._json(400, {"error": "filename must be a video (mp4/mov/mkv/h264)"})
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            self._json(400, {"error": "empty upload"}); return
        dst = c.media_dir / name
        descriptor, temporary = tempfile.mkstemp(dir=c.media_dir, prefix='.upload-', suffix='.part')
        try:
            with os.fdopen(descriptor, "wb") as f:
                left = n
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise ValueError('upload ended before all declared bytes arrived')
                    f.write(chunk); left -= len(chunk)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, dst)
        finally:
            Path(temporary).unlink(missing_ok=True)
        print(f"[ctl] uploaded {name} ({n} bytes)")
        asyncio.run_coroutine_threadsafe(
            c._broadcast_web({"type": "playlist", "playlist": c.playlist()}), c._loop)
        self._json(200, {"uploaded": name, "playlist": c.playlist()})

    def _serve_media(self, name: str):
        import os, shutil
        from urllib.parse import unquote
        f = self.controller.media_dir / os.path.basename(unquote(name))
        try:
            fh = open(f, "rb")
        except (FileNotFoundError, IsADirectoryError):
            self.send_error(404); return
        with fh:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(os.fstat(fh.fileno()).st_size))
            self.end_headers()
            shutil.copyfileobj(fh, self.wfile)

    # ---- per-node admin (Basic auth, same page every node serves) --------
    def _basic_ok(self) -> bool:
        if nodeadmin.check_auth(self.headers.get("Authorization"),
                                self.controller.admin_password):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="immersive", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _admin_get(self, path) -> bool:
        if path == "/admin":
            self.send_response(302)
            self.send_header("Location", "/admin/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        if path.startswith("/admin/api/"):
            path = path[len("/admin"):]
        if path in ("/admin", "/admin/"):
            if not self._basic_ok():
                return True
            body = nodeadmin.ADMIN_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return True
        if path == "/api/health":
            if self._basic_ok():
                self._json(200, self.controller.admin_health())
            return True
        if path == "/api/config":
            if self._basic_ok():
                self._json(200, {"config": nodeadmin.read_bootconfig(),
                                 "editable": nodeadmin.EDITABLE})
            return True
        if path == "/api/diagnostics":
            if self._basic_ok():
                self._json(200, {"show": self.controller.show,
                    "nodes_up": list(self.controller.nodes),
                    "heartbeats": self.controller.heartbeats,
                    "node_info": self.controller.node_info,
                    "events": list(self.controller.events.events)})
            return True
        return False

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/ws":
            self._proxy_websocket()
            return
        if self._admin_get(path):
            return
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

    def _proxy_websocket(self):
        """Expose browser control on the same origin/port as the web page."""
        import select
        import socket
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self.send_error(400, "WebSocket upgrade required")
            return
        self.close_connection = True
        try:
            with socket.create_connection(("127.0.0.1", self.controller.ws_port), timeout=5) as backend:
                backend.settimeout(None)
                headers = ["GET / HTTP/1.1", f"Host: 127.0.0.1:{self.controller.ws_port}"]
                headers += [f"{key}: {value}" for key, value in self.headers.items()
                            if key.lower() not in ("host", "authorization", "cookie")]
                backend.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1"))
                while True:
                    readable, _, _ = select.select([self.connection, backend], [], [], 30)
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        target = backend if source is self.connection else self.connection
                        target.sendall(data)
        except (OSError, ValueError):
            return

    def log_message(self, *a):
        pass  # quiet; the controller prints its own lines


def _start_http(controller: "Controller"):
    if not WEB_DIR.exists():
        WEB_DIR.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(_ApiHandler, directory=str(WEB_DIR))
    # bind the controller onto the handler class the partial wraps
    _ApiHandler.controller = controller
    httpd = make_http_server(controller.http_port, handler)
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
