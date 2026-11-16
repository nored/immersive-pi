"""Clocked frame renderer with decode-ahead, native KMS staging.

The main thread owns GLES/KMS. A background WebSocket loop exchanges numbered
stage/commit commands and acknowledgements with the master. All members must
stage a frame before its shared presentation deadline is committed. On failure
the session holds/stops explicitly; it never seeks or skips frames to catch up.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import gc
import json
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path

from player import Player
from presenter import Presenter
from shared.timing_trace import TimingTrace
from shared.frame_sync import PROTOCOL_VERSION
from shared.node_health import collect as collect_hardware_health
from shared.service_health import notify
from shared.media_package import validate_tile_coverage
from gl_pipeline import GLStage, KmsDisplay
import nodeadmin

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None




class RenderNode:
    def __init__(self, cfg: dict):
        self.trace = TimingTrace()
        self.cfg = cfg
        self.node = cfg["node"]
        self.control_host = cfg["control_host"]
        self.ws_url = f"ws://{cfg['control_host']}:{cfg['control_ws_port']}"
        self.control_http_port = cfg.get("control_http_port", 8080)
        self.media_dir = Path(cfg.get("media_dir", "."))
        self.hb_hz = cfg.get("heartbeat_hz", 1)
        # only the real image config sets this true — never poweroff a dev box
        self.allow_poweroff = bool(cfg.get("allow_poweroff", False))
        self.allow_reboot = bool(cfg.get("allow_reboot", Path("/etc/immersive-version").is_file()))

        self.player = Player(decoder=cfg.get("decoder", "v4l2h264dec"),
                             buffer_frames=cfg.get("buffer_frames", 24))
        self.player.trace = self.trace
        self.display: KmsDisplay | None = None
        self.stage: GLStage | None = None

        self._cmd_q: queue.Queue = queue.Queue()
        self._out_q: queue.Queue = queue.Queue(maxsize=128)
        self._session = None
        self._staged_frames = {}
        self._stage_requests = deque()
        self._stage_budget_ns = 25_000_000
        self._timings = {}
        self._present_lock = threading.RLock()
        self._presenter = None
        self._gc_frozen = False
        self._last_presented_frame = None
        self._last_commit = -1
        self._presented = -1
        self._presentation_error_ns = None
        self._playback_error = None
        self._media_name = None
        self._graphics_info = {}
        self._hardware_health = {}
        self._entry: dict | None = None
        self._blackout = False
        self._standby = False        # soft hibernation: on the net, output off
        self._scan = None            # (axis, bit, inverted) while scanning, else None
        self._scan_key = None        # cache so we only regenerate on change
        self._capture_display = None # phone scan: raw target + DRM receipt
        self._identify_until = 0.0
        self._identify_prev = ("video", False)
        self._running = True
        self._ws_connected = False
        self._fps = 0.0
        self._frame_count = 0
        self._clock_port = cfg.get("clock_port", 8555)
        self.admin_port = cfg.get("admin_port", 8080)
        self._display_error = None

    # ---- display bring-up ------------------------------------------------
    def init_display(self):
        drm = self.cfg.get("drm", {})
        self.display = KmsDisplay(drm.get("device", "/dev/dri/card0"),
                                  drm.get("connector", "auto"), drm.get("mode"))
        w, h = self.display.setup()
        self.display.make_current()
        self.stage = GLStage(w, h)
        self.stage.init_gl()
        self._graphics_info = self.stage.graphics_info()
        if self._entry:
            self.stage.set_entry(self._entry)
        print(f"[{self.node}] display up {w}x{h}")

    # ---- command application (runs on render thread) ---------------------
    def _apply(self, msg: dict):
        cmd = msg.get("cmd")
        if cmd in ("prepare", "stop", "sleep"):
            self._stop_presenter()
            self._resume_gc()
        if cmd == "presentation_error":
            if msg.get("session", self._session) != self._session:
                return
            self._fail_playback(msg["error"])
            return
        if self._presenter and cmd in ("identify", "testpattern", "scan_show", "blackout", "capture_pattern"):
            raise RuntimeError("stop synchronized playback before static display commands")
        if cmd in ("prepare", "stop", "sleep", "identify", "testpattern", "blackout", "scan_show"):
            self._capture_display = None
        if cmd in ("stage_frame", "commit_frame"):
            self.trace.record("command_apply", cmd=cmd, sequence=msg.get("sequence"))
        if cmd == "set_mesh":
            self._merge_entry("mesh", msg["mesh"])
        elif cmd == "set_blend":
            self._merge_entry("blend", msg["blend"])
        elif cmd == "set_color":
            self._merge_entry("color", msg["color"])
        elif cmd == "set_source_region":
            self._merge_entry("source_region", msg["source_region"])
        elif cmd == "testpattern":
            self.stage and self.stage.set_pattern(
                msg.get("kind", "grid"), msg.get("on", True),
                tuple(msg.get("color", (1.0, 0.0, 0.0))))
        elif cmd == "identify":
            self._identify_until = time.monotonic() + msg.get("ms", 2000) / 1000.0
        elif cmd == "capture_pattern":
            self._set_capture_pattern(msg)
        elif cmd == "prepare":
            if self.display is None or self.stage is None:
                raise RuntimeError(self._display_error or "projector display is unavailable")
            self._identify_until = 0
            self._scan = None
            self._staged_frames.clear()
            self.display.discard_staged_frames()
            self._stage_requests.clear()
            self._last_commit = self._presented = -1
            self._playback_error = None
            self._session = msg.get("session")
            if msg.get("protocol") != PROTOCOL_VERSION or not self._session:
                raise RuntimeError("frame protocol v2 requires an upgraded master")
            if self.stage is None:
                raise RuntimeError(self._display_error or "projector display is unavailable")
            validate_tile_coverage(self._entry, msg.get("video_region"))
            self._media_name = msg.get("slice", "test.mp4")
            self._clock_port = msg.get("clock_port", self._clock_port)
            media = self._ensure_media(self._media_name, msg.get("sha256"))
            loop = self.cfg.get("loop", True)
            info = self.player.prepare(str(media), loop=loop)
            self.player.slave_to_clock(self.control_host, self._clock_port)
            self._blackout = False
            self.stage.set_video_region(msg.get("video_region"))
            self.stage.set_pattern("video", False)
            # Collect the large imported/GI object graph while no frames are
            # scheduled. Freeze that stable graph so later full collections do
            # not stall every Python thread for 40–60ms during scanout. New
            # objects still receive normal cyclic GC; stop unfreezes the graph.
            gc.collect()
            gc.freeze()
            self._gc_frozen = True
            self._presenter = Presenter(self.display, self.player.clock_time_ns,
                self._complete_presentation,
                lambda error, session=self._session: self._cmd_q.put_nowait({"cmd": "presentation_error", "error": error, "session": session}),
                trace=self.trace.record if self.trace.path else None)
            self._out_q.put_nowait({"type": "ready", "session": self._session,
                                    "spec": info})
        elif cmd == "stage_frame":
            if msg.get("session") != self._session:
                return
            self._stage_requests.append(msg)
        elif cmd == "commit_frame":
            self._receive_commit(msg)
        elif cmd in ("play_at", "loop"):
            raise RuntimeError("legacy seek-based playback is disabled; upgrade the master to frame protocol v2")
        elif cmd == "stop":
            self._session = None
            self._staged_frames.clear()
            self._stage_requests.clear()
            if self.display and self.stage:
                self.display.discard_staged_frames()
                if self._last_presented_frame:
                    self.stage.upload_frame(self._last_presented_frame)
            self.player.stop()
        elif cmd == "blackout":
            self._blackout = bool(msg.get("on", True))
        elif cmd == "scan_show":
            # structured-light: show one gray-code bitplane raw, or leave scan mode
            if msg.get("on", True):
                self._scan = (msg.get("axis"), msg.get("bit"), bool(msg.get("inverted", False)))
            else:
                self._scan = None
                self._scan_key = None
        elif cmd == "sleep":
            self._sleep(bool(msg.get("poweroff", True)),
                        standby=bool(msg.get("standby", False)))
        elif cmd == "wake":
            self._wake()
        elif cmd == "adopt":
            self._adopt(msg.get("node"), msg.get("role", "render"))

    def _ensure_media(self, name: str, expected_sha=None, timeout=60) -> Path:
        """Make sure the named clip is on this node; if not, pull it from the
        control node's /media/ endpoint. Lets an operator upload once on the
        website and have every node fetch it — no per-node copying."""
        import hashlib
        from urllib.parse import quote
        if not name or Path(name).name != name:
            raise ValueError("media must be a plain filename")
        path = self.media_dir / name
        def matches():
            if not path.exists():
                return False
            if expected_sha is None:
                return True
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest() == expected_sha
        if matches():
            return path
        self.media_dir.mkdir(parents=True, exist_ok=True)
        url = f"http://{self.control_host}:{self.control_http_port}/media/{quote(name)}"
        try:
            import urllib.request
            tmp = path.with_suffix(path.suffix + ".part")
            import shutil
            with urllib.request.urlopen(url, timeout=timeout) as source, tmp.open("wb") as target:
                shutil.copyfileobj(source, target)
            if expected_sha:
                with tmp.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha:
                        raise RuntimeError("downloaded video checksum does not match the master's file")
            tmp.replace(path)
            print(f"[{self.node}] fetched {name} from control node")
        except Exception as e:
            print(f"[{self.node}] could not fetch {name} ({e})")
            raise
        return path

    def _merge_entry(self, field: str, value):
        candidate = copy.deepcopy(self._entry) if self._entry else {"node": self.node}
        candidate[field] = value
        if self._session and self.stage and field in ("mesh", "source_region"):
            validate_tile_coverage(candidate, self.stage._video_region)
        self._entry = candidate
        if self.stage and field in ("mesh", "source_region"):
            self.stage.set_entry(self._entry)
        elif self.stage:
            # blend/color are uniform-only; set_entry would also rebuild the mesh
            self.stage._entry = self._entry

    # ---- render loop (main thread) --------------------------------------
    def _set_capture_pattern(self, msg):
        import re
        session=msg.get("session")
        if not isinstance(session,str) or not re.fullmatch(r"[0-9a-f]{32}",session):
            raise ValueError("invalid phone scan session")
        if msg.get("kind")=="clear":
            if self._capture_display and self._capture_display["session"]==session:
                self._capture_display=None
                if self._last_presented_frame is not None:
                    self.stage.upload_frame(self._last_presented_frame)
                elif self.stage._pattern==0:
                    self.stage.upload_raw(bytes(self.stage.width*self.stage.height*4),self.stage.width,self.stage.height)
            return
        if self.stage is None or self.display is None or self._standby:
            raise ValueError("projector display is not ready for phone scanning")
        kind=msg.get("kind")
        if kind not in ("fiducials","white","black") or type(msg.get("phase")) is not int or msg["phase"]<1:
            raise ValueError("invalid phone scan phase")
        if msg.get("width")!=self.stage.width or msg.get("height")!=self.stage.height:
            raise ValueError("phone scan target does not match native output dimensions")
        if kind=="fiducials":
            from PIL import Image
            sha=msg.get("sha256")
            if not isinstance(sha,str) or not re.fullmatch(r"[0-9a-f]{64}",sha):
                raise ValueError("scan target checksum required")
            path=self._ensure_media(msg.get("file"),sha,timeout=5)
            with Image.open(path) as image:
                if image.size!=(self.stage.width,self.stage.height):
                    raise ValueError("scan target image dimensions are wrong")
                rgba=image.convert("RGBA").tobytes()
        else:
            import numpy as np
            image=np.full((self.stage.height,self.stage.width,4),255 if kind=="white" else 0,dtype=np.uint8)
            image[:,:,3]=255
            rgba=image.tobytes()
        self.stage.upload_raw(rgba,self.stage.width,self.stage.height)
        self._scan=None
        self._identify_until=0
        self._capture_display={"session":session,"phase":msg["phase"],"kind":kind,
                               "width":self.stage.width,"height":self.stage.height,
                               "sha256":msg.get("sha256"),"ack_pending":True}

    def _capture_flip_receipt(self, timestamp):
        current=self._capture_display
        if current and current["ack_pending"]:
            try:
                self._out_q.put_nowait({"type":"capture_displayed","node":self.node,
                    **{k:v for k,v in current.items() if k!="ack_pending"},
                    "flip_monotonic_ns":timestamp})
                current["ack_pending"]=False
            except queue.Full:
                pass

    def render_loop(self):
        next_hb = 0.0
        next_sync = 0.0
        hb_interval = 1.0 / max(self.hb_hz, 1)
        last_loop_ns, last_cpu_ns = time.monotonic_ns(), time.thread_time_ns()
        while self._running:
            if self.trace.path:
                loop_ns, cpu_ns = time.monotonic_ns(), time.thread_time_ns()
                if loop_ns-last_loop_ns > 25_000_000:
                    self.trace.record("loop_gap", elapsed_ms=(loop_ns-last_loop_ns)/1e6,
                                      cpu_ms=(cpu_ns-last_cpu_ns)/1e6,
                                      sequence=self._presented)
                last_loop_ns, last_cpu_ns = loop_ns, cpu_ns
            # apply any pending commands
            try:
                while True:
                    msg = self._cmd_q.get_nowait()
                    try:
                        self._apply(msg)
                    except Exception as exc:
                        if msg.get("cmd")=="capture_pattern":
                            self._capture_display=None
                            try:self._out_q.put_nowait({"type":"capture_error","node":self.node,
                                "session":msg.get("session"),"phase":msg.get("phase"),"error":str(exc)})
                            except queue.Full:pass
                        else:self._fail_playback(str(exc))
            except queue.Empty:
                pass

            if self._standby:
                # soft hibernation: output is off, stay idle but keep heartbeats
                now = time.monotonic()
                if now >= next_hb:
                    next_hb = now + hb_interval
                    self._emit_heartbeat()
                time.sleep(0.1)
                continue

            if self.stage is None:
                now = time.monotonic()
                if now >= next_hb:
                    next_hb = now + hb_interval
                    self._emit_heartbeat()
                time.sleep(0.05)
                continue

            self.display.make_current()

            if self._capture_display is not None:
                self.stage.render_raw()
                if self._capture_display["ack_pending"] and "gpu_rgb_samples" not in self._capture_display:
                    try:self._capture_display["gpu_rgb_samples"]=self.stage.sample_scan_rgb()
                    except Exception as exc:
                        self._capture_display["gpu_rgb_samples"]={"error":str(exc),"optical_output_verified":False}
                timestamp=self.display.swap_and_flip()
                self._capture_flip_receipt(timestamp)
                now=time.monotonic()
                if now>=next_hb:
                    next_hb=now+hb_interval
                    self._emit_heartbeat()
                continue

            if self._scan is not None:
                # structured-light scan: project the raw bitplane, nothing else
                self._render_scan()
                self.display.swap_and_flip()
                now = time.monotonic()
                if now >= next_hb:
                    next_hb = now + hb_interval
                    self._emit_heartbeat()
                continue

            self._handle_identify()
            if self._blackout:
                gl = self.stage.gl
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
                gl.glViewport(0, 0, self.stage.width, self.stage.height)
                gl.glClearColor(0, 0, 0, 1)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)
                self.display.swap_and_flip()
            elif not self._session or self.stage._pattern != 0 or self._identify_until:
                self.stage.render_screen()
                self.display.swap_and_flip()
            else:
                time.sleep(0.001)

            now = time.monotonic()
            if self._stage_requests and self._has_time_for(self._stage_budget_ns):
                request = self._stage_requests.popleft()
                if request.get("session") == self._session:
                    try:
                        sequence = int(request["sequence"])
                        frame = self.player.take_frame(sequence)
                        if frame is None:
                            raise RuntimeError(f"reserved frame {sequence} is not decoded")
                        started = time.monotonic_ns()
                        self.stage.draw_frame(frame)
                        drawn = time.monotonic_ns()
                        self.display.stage_frame(sequence)
                        if self.trace.path:
                            self.trace.record("stage_done", sequence=sequence, draw_ms=(drawn-started)/1e6, native_ms=(time.monotonic_ns()-drawn)/1e6, buffered=self.player.buffered_range())
                        elapsed = time.monotonic_ns() - started
                        self._record_timing("stage", elapsed)
                        self._stage_budget_ns = max(15_000_000, min(30_000_000, int(elapsed * 1.3)))
                        self._staged_frames[sequence] = frame
                        self._out_q.put_nowait({"type": "frame_staged", "session": self._session,
                                                "sequence": sequence})
                    except Exception as exc:
                        self._fail_playback(str(exc))
            if now >= next_hb:
                next_hb = now + hb_interval
                self._emit_heartbeat()
            if self._session and now >= next_sync:
                next_sync = now + 0.02
                if self.player.error and not self._playback_error:
                    self._fail_playback(self.player.error)
                first, last = self.player.buffered_range()
                try:
                    self._out_q.put_nowait({"type": "frame_status", "session": self._session,
                        "buffered_first": first, "buffered_last": last,
                        "presented": self._presented, "presentation_error_ns": self._presentation_error_ns,
                        "error": self._playback_error})
                except queue.Full:
                    self._fail_playback("control telemetry queue overflow")

    def _fail_playback(self, error):
        if error != self._playback_error:
            print(f"[{self.node}] playback failed: {error}")
        self._playback_error = error
        self._stop_presenter()
        self._stage_requests.clear()
        try:
            self._out_q.put_nowait({"type": "node_error", "session": self._session,
                                    "error": error})
        except queue.Full:
            pass

    def _receive_commit(self, msg):
        # Clocked flip commands must never wait behind a long GLES upload/draw.
        # The network thread can submit a frame once GPU staging has completed.
        with self._present_lock:
            if msg.get("session") != self._session or self._presenter is None:
                return
            sequence = int(msg["sequence"])
            if sequence != self._last_commit + 1:
                raise RuntimeError(f"non-contiguous frame commit: {sequence} after {self._last_commit}")
            if int(msg["frame"]) != sequence % self.player.spec["frames"]:
                raise RuntimeError("frame commit disagrees with video frame count")
            if sequence not in self._staged_frames:
                raise RuntimeError("master committed a frame before GPU staging completed")
            frame = self._staged_frames.pop(sequence)
            self._presenter.submit(frame, msg, self._session)
            self._last_commit = sequence
            self.trace.record("commit_dispatched", sequence=sequence)

    def _stop_presenter(self):
        with self._present_lock:
            presenter, self._presenter = self._presenter, None
        if presenter:
            presenter.stop()

    def _resume_gc(self):
        if self._gc_frozen:
            gc.unfreeze()
            self._gc_frozen = False

    def _has_time_for(self, duration_ns):
        # DRM timing has its own worker; GPU staging can span a display deadline.
        return True

    def _record_timing(self, name, elapsed_ns):
        self._timings[name + "_ms"] = round(elapsed_ns / 1e6, 3)
        self._timings[name + "_max_ms"] = max(self._timings.get(name + "_max_ms", 0), round(elapsed_ns / 1e6, 3))

    def _complete_presentation(self, frame, commit, session, flip_master_ns):
        if session != self._session or self.player.net_clock is None:
            return
        self.player.mark_presented(frame)
        self._last_presented_frame = frame
        self._presented = frame.sequence
        # Presenter converts the DRM timestamp once for scheduling and reporting.
        self._presentation_error_ns = flip_master_ns - commit["present_at_ns"]
        self.trace.record("presented", sequence=frame.sequence, clock_ns=flip_master_ns, deadline_ns=commit["present_at_ns"], error_ns=self._presentation_error_ns)
        self._out_q.put_nowait({"type": "frame_presented", "session": session,
            "sequence": frame.sequence, "frame": commit["frame"],
            "presented_at_ns": flip_master_ns,
            "presentation_error_ns": self._presentation_error_ns})
        self._frame_count += 1

    def _adopt(self, node: str, role: str):
        """Adopt an identity assigned from the website: write immersive.conf on
        the boot partition (role + node id + control host) and reboot. The node
        keeps its DHCP address; it becomes reachable as <node>.local via mDNS."""
        if not self.allow_reboot:
            print(f"[{self.node}] adopt -> {node}/{role} "
                  f"(no-op; system writes disabled on this host)")
            return
        try:
            nodeadmin.write_bootconfig({"role": role, "node": node,
                                       "hostname": node, "control_host": self.control_host})
            print(f"[{self.node}] adopted {node}/{role}; rebooting to apply")
            import subprocess
            subprocess.Popen(["systemctl", "reboot"])
        except Exception as e:
            print(f"[{self.node}] adopt failed: {e}")

    def _sleep(self, poweroff: bool, standby: bool = False):
        """Hibernate this node. Three modes:
          - standby: stay powered and on the network, blank the projector output
            (drops it to no-signal/standby) and idle the CPU; woken by a 'wake'
            message. For nodes on constant power (no switch to cold-boot them).
          - poweroff: clean-poweroff to the bootloader's low-power halt; woken by
            external switched power or a GPIO pulse.
          - neither: just blank (dev boxes)."""
        self._scan = None
        self._blackout = True
        self.player.stop()
        if standby and not poweroff:
            self._standby = True
            self._set_governor("powersave")
            if self.display:
                self.display.blank()      # projector -> no signal -> standby
            print(f"[{self.node}] standby -> output off, on network, wakeable")
            return
        if poweroff and self.allow_poweroff:
            import subprocess
            print(f"[{self.node}] hibernate -> systemctl poweroff")
            try:
                subprocess.Popen(["systemctl", "poweroff"])
            except Exception as e:
                print(f"[{self.node}] poweroff failed: {e}")
        else:
            print(f"[{self.node}] hibernate -> blanked (poweroff disabled)")

    def _wake(self):
        """Exit standby: restore clocks and the projector output. The control
        node re-sends prepare/play to resume the show; the next render_loop frame
        re-runs the full modeset (display.blank cleared _prev_bo)."""
        if not self._standby:
            return
        self._standby = False
        self._blackout = False
        self._set_governor("performance")
        print(f"[{self.node}] wake -> output on, resuming")

    def _set_governor(self, gov: str):
        try:
            with open("/sys/devices/system/cpu/cpufreq/policy0/scaling_governor", "w") as f:
                f.write(gov)
        except Exception:
            pass

    def _render_scan(self):
        if self._scan != self._scan_key:
            from scanpattern import make_pattern
            axis, bit, inv = self._scan
            w, h = self.stage.width, self.stage.height
            rgba = make_pattern(axis, bit, inv, w, h)
            self.stage.upload_raw(rgba, w, h)
            self._scan_key = self._scan
        self.stage.render_raw()

    def _handle_identify(self):
        if self._identify_until and time.monotonic() < self._identify_until:
            self.stage.set_pattern("white", True)
        elif self._identify_until:
            self._identify_until = 0.0
            self.stage.set_pattern("video", False)



    def _emit_heartbeat(self):
        heartbeat_started = time.monotonic_ns()
        notify("WATCHDOG=1")
        now = time.monotonic()
        dt = now - getattr(self, "_hb_t", now)
        if dt > 0:
            self._fps = (self._frame_count - getattr(self, "_hb_frames", 0)) / dt
        self._hb_t = now
        self._hb_frames = self._frame_count
        hb = {
            "node": self.node,
            "renderer_mode": "trial" if os.environ.get("IMMERSIVE_TRIAL") == "1" else "production",
            "session": self._session,
            "media_sha256": (self.player.spec or {}).get("sha256"),
            "clock_offset_ns": self.player.clock_offset_ns(),
            "media_pos_ns": self.player.media_pos_ns(),
            "decoder_ok": self.player.decoder_ok(),
            "fb_ok": self.stage is not None,
            "temp_c": self._hardware_health.get("temperature_c"),
            "display_error": self._display_error,
            "render_fps": round(self._fps, 2),
            "playback_error": self._playback_error,
            "allow_poweroff": self.allow_poweroff, "allow_reboot": self.allow_reboot,
            "presented_sequence": self._presented,
            "presentation_error_ns": self._presentation_error_ns,
            "media": self._media_name,
            "graphics": self._graphics_info,
            "hardware": self._hardware_health,
            "timings": self._timings,
            "video_fps_num": (self.player.spec or {}).get("fps_num"),
            "video_fps_den": (self.player.spec or {}).get("fps_den"),
            "decoder_restarts": self.player.decoder_restarts,
            "decoder_restart_ms": self.player.decoder_restart_ms,
            "capture_display": {k:v for k,v in self._capture_display.items() if k!="ack_pending"} if getattr(self,"_capture_display",None) else None,
        }
        try:
            self._out_q.put_nowait(hb)
        except queue.Full:
            pass

        self.trace.record("heartbeat_done", elapsed_ms=(time.monotonic_ns()-heartbeat_started)/1e6)

    # ---- websocket client (background thread) ---------------------------
    async def _ws_main(self):
        assert websockets is not None, "pip install websockets"
        while self._running:
            try:
                async with websockets.connect(self.ws_url, compression=None, max_size=4 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({"role": "node", "hello": self.node,
                                              "protocol": PROTOCOL_VERSION,
                                              "capabilities": ["per_node_video_v1", "phone_scan_v1"],
                                              "mac": _node_mac(), "serial": _node_serial()}))
                    self._ws_connected = True
                    print(f"[{self.node}] connected to {self.ws_url}")
                    recv = asyncio.create_task(self._ws_recv(ws))
                    send = asyncio.create_task(self._ws_send(ws))
                    await asyncio.wait([recv, send], return_when=asyncio.FIRST_COMPLETED)
                    recv.cancel(); send.cancel()
            except Exception as e:
                print(f"[{self.node}] ws down ({e}); retry in 2s (still rendering)")
            self._ws_connected = False
            await asyncio.sleep(2)

    async def _ws_recv(self, ws):
        async for raw in ws:
            try:
                message = json.loads(raw)
                if message.get("cmd") in ("stage_frame", "commit_frame"):
                    self.trace.record("command_received", cmd=message["cmd"], sequence=message.get("sequence"))
                if message.get("cmd") == "commit_frame":
                    try:
                        self._receive_commit(message)
                    except Exception as exc:
                        self._cmd_q.put_nowait({"cmd": "presentation_error", "error": str(exc),
                                               "session": message.get("session")})
                else:
                    self._cmd_q.put_nowait(message)
            except (ValueError, TypeError):
                pass

    async def _ws_send(self, ws):
        while True:
            try:
                msg = self._out_q.get_nowait()
            except queue.Empty:
                msg = None
            if msg is not None:
                await ws.send(json.dumps(msg))
            else:
                await asyncio.sleep(0.002)

    def start_ws_thread(self):
        def run():
            asyncio.run(self._ws_main())
        threading.Thread(target=run, daemon=True).start()

    # ---- run -------------------------------------------------------------
    def _advertise_mdns(self):
        """Publish this node as an _immersive._tcp service via avahi, so it can
        be found on the network with `avahi-browse -rt _immersive._tcp` (the
        <node>.local A-record is advertised by avahi automatically). No effect
        off a real node."""
        if not self.allow_reboot:
            return
        d = Path("/etc/avahi/services")
        if not d.is_dir():
            return
        xml = (
            "<?xml version=\"1.0\" standalone='no'?>\n"
            "<!DOCTYPE service-group SYSTEM \"avahi-service.dtd\">\n"
            "<service-group>\n"
            "  <name replace-wildcards=\"yes\">immersive %h</name>\n"
            "  <service>\n"
            "    <type>_immersive._tcp</type>\n"
            "    <port>22</port>\n"
            f"    <txt-record>node={self.node}</txt-record>\n"
            f"    <txt-record>mac={_node_mac()}</txt-record>\n"
            f"    <txt-record>serial={_node_serial()}</txt-record>\n"
            "  </service>\n"
            "</service-group>\n")
        try:
            (d / "immersive.service").write_text(xml)
            print(f"[{self.node}] advertised _immersive._tcp via mDNS")
        except Exception as e:
            print(f"[{self.node}] mDNS advertise failed: {e}")

    def _health(self) -> dict:
        up = 0.0
        try:
            up = float(open("/proc/uptime").read().split()[0])
        except Exception:
            pass
        return {
            "node": self.node, "role": "render", "hostname": _hostname(),
            "version": _version(), "uptime_s": int(up),
            "temp_c": self._hardware_health.get("temperature_c"), "ip": _node_ip(), "mac": _node_mac(),
            "control_host": self.control_host, "connected": self._ws_connected,
            "decoder_ok": self.player.decoder_ok(), "fb_ok": self.stage is not None,
            "clock_offset_ms": round(self.player.clock_offset_ns() / 1e6, 3),
            "media_pos_s": round(self.player.media_pos_ns() / 1e9, 2),
            "fps": round(self._fps, 1),
            "hardware": self._hardware_health, "graphics": self._graphics_info,
            "display_error": self._display_error,
            "playback_error": self._playback_error,
        }

    def _hardware_health_loop(self):
        refresh = 0
        while self._running:
            try:
                self._hardware_health = collect_hardware_health(include_kernel=refresh % 12 == 0)
            except Exception as exc:
                self._hardware_health = {"error": str(exc)}
            refresh += 1
            for _ in range(50):
                if not self._running:
                    return
                time.sleep(.1)

    def run(self):
        threading.Thread(target=self._hardware_health_loop, daemon=True).start()
        self._advertise_mdns()
        try:
            nodeadmin.make_admin_server(self.admin_port,
                                        nodeadmin.admin_password(self.node),
                                        self._health, allow_reboot=self.allow_reboot)
            print(f"[{self.node}] admin web on :{self.admin_port}/admin")
        except Exception as e:
            print(f"[{self.node}] admin web unavailable: {e}")
        self.start_ws_thread()
        try:
            self.init_display()
        except Exception as e:
            self._display_error = str(e)
            self.stage = None
            print(f"[{self.node}] display bring-up failed ({e}). "
                  f"Running control-plane only; confirm GLES2-over-GBM on this board.")
        notify("READY=1")
        try:
            self.render_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            self._stop_presenter()
            self._resume_gc()
            self.player.stop()
            if self.display:
                self.display.teardown()


def _hostname() -> str:
    import socket
    return socket.gethostname()


def _version() -> str:
    try:
        return open("/etc/immersive-version").read().strip()
    except Exception:
        return "dev"


def _node_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))   # TEST-NET addr; no packets sent
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()


def _node_mac() -> str:
    """MAC of the wired interface — the key the control node's DHCP reserves on."""
    import glob
    for path in ["/sys/class/net/eth0/address"] + sorted(glob.glob("/sys/class/net/*/address")):
        try:
            iface = path.split("/")[-2]
            if iface == "lo":
                continue
            mac = open(path).read().strip().lower()
            if mac and mac != "00:00:00:00:00:00":
                return mac
        except Exception:
            continue
    return ""


def _node_serial() -> str:
    """Raspberry Pi serial number, for identifying a node before it's named."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    try:
        return open("/sys/firmware/devicetree/base/serial-number").read().strip("\x00").strip()
    except Exception:
        return ""


def _soc_temp_c() -> float:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return 0.0


def load_config(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--node", help="override node id from config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.node:
        cfg["node"] = args.node
    RenderNode(cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
