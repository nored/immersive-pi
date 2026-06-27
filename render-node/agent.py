"""agent.py — render node entry point.

Ties the three render-node pieces together:
  * player.py      decodes, slaved to the net clock;
  * gl_pipeline.py warps/blends/colours each frame to the projector via KMS, and
    renders a small preview FBO;
  * this file      is the WebSocket client to the control node: it applies
    set_mesh / set_blend / set_color / testpattern / identify / prepare /
    play_at / stop / blackout, sends 1 Hz heartbeats and ~8 fps preview JPEGs.

Threading: GLES2 must stay on one thread, so the GL render loop runs on the main
thread and the asyncio WebSocket client runs in a background thread. Inbound
commands and outbound telemetry cross between them on plain thread-safe queues.

The render loop keeps looping on the last base time even if the controller is
away (master reboot must not black the room), and reconnects when it returns.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import queue
import threading
import time
from pathlib import Path

from player import Player
from gl_pipeline import GLStage, KmsDisplay

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None


def _jpeg(rgba: bytes, w: int, h: int, quality: int) -> bytes:
    """RGBA bytes -> JPEG. Uses turbojpeg if available, else Pillow."""
    from PIL import Image
    img = Image.frombytes("RGBA", (w, h), rgba).convert("RGB")
    # FBO readback is bottom-row-first; flip so previews are upright.
    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class RenderNode:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.node = cfg["node"]
        self.control_host = cfg["control_host"]
        self.ws_url = f"ws://{cfg['control_host']}:{cfg['control_ws_port']}"
        self.control_http_port = cfg.get("control_http_port", 8080)
        self.media_dir = Path(cfg.get("media_dir", "."))
        pv = cfg.get("preview", {})
        self.pv_w = pv.get("width", 320)
        self.pv_h = pv.get("height", 180)
        self.pv_fps = pv.get("fps", 8)
        self.pv_q = pv.get("jpeg_quality", 60)
        self.hb_hz = cfg.get("heartbeat_hz", 1)
        # only the real image config sets this true — never poweroff a dev box
        self.allow_poweroff = bool(cfg.get("allow_poweroff", False))

        self.player = Player()
        self.display: KmsDisplay | None = None
        self.stage: GLStage | None = None

        self._cmd_q: queue.Queue = queue.Queue()
        self._out_q: queue.Queue = queue.Queue(maxsize=8)
        self._entry: dict | None = None
        self._blackout = False
        self._scan = None            # (axis, bit, inverted) while scanning, else None
        self._scan_key = None        # cache so we only regenerate on change
        self._identify_until = 0.0
        self._identify_prev = ("video", False)
        self._running = True
        self._clock_port = cfg.get("clock_port", 8555)

    # ---- display bring-up ------------------------------------------------
    def init_display(self):
        drm = self.cfg.get("drm", {})
        self.display = KmsDisplay(drm.get("device", "/dev/dri/card0"),
                                  drm.get("connector", "auto"))
        w, h = self.display.setup()
        self.display.make_current()
        self.stage = GLStage(w, h, self.pv_w, self.pv_h)
        self.stage.init_gl()
        if self._entry:
            self.stage.set_entry(self._entry)
        print(f"[{self.node}] display up {w}x{h}")

    # ---- command application (runs on render thread) ---------------------
    def _apply(self, msg: dict):
        cmd = msg.get("cmd")
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
        elif cmd == "prepare":
            media = self._ensure_media(msg.get("slice", "test.mp4"))
            loop = self.cfg.get("loop", True)
            self.player.prepare(str(media), loop=loop)
            self.player.slave_to_clock(self.control_host, self._clock_port)
        elif cmd == "play_at":
            self._clock_port = msg.get("clock_port", self._clock_port)
            self.player.play_at(msg["base_time_ns"], self.control_host, self._clock_port)
            if "loop_epoch_ns" in msg:
                self.player.set_loop_epoch(msg["loop_epoch_ns"])
        elif cmd == "loop":
            self.player.set_loop_epoch(msg["loop_epoch_ns"])
        elif cmd == "stop":
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
            self._sleep(bool(msg.get("poweroff", True)))
        elif cmd == "adopt":
            self._adopt(msg.get("node"), msg.get("role", "render"), msg.get("ip"))

    def _ensure_media(self, name: str) -> Path:
        """Make sure the named clip is on this node; if not, pull it from the
        control node's /media/ endpoint. Lets an operator upload once on the
        website and have every node fetch it — no per-node copying."""
        path = self.media_dir / name
        if path.exists():
            return path
        self.media_dir.mkdir(parents=True, exist_ok=True)
        url = f"http://{self.control_host}:{self.control_http_port}/media/{name}"
        try:
            import urllib.request
            tmp = path.with_suffix(path.suffix + ".part")
            urllib.request.urlretrieve(url, tmp)
            tmp.replace(path)
            print(f"[{self.node}] fetched {name} from control node")
        except Exception as e:
            print(f"[{self.node}] could not fetch {name} ({e})")
        return path

    def _merge_entry(self, field: str, value):
        if self._entry is None:
            self._entry = {"node": self.node}
        self._entry[field] = value
        if self.stage and field in ("mesh", "source_region"):
            self.stage.set_entry(self._entry)
        elif self.stage:
            # blend/color are uniform-only; set_entry would also rebuild the mesh
            self.stage._entry = self._entry

    # ---- render loop (main thread) --------------------------------------
    def render_loop(self):
        next_pv = 0.0
        next_hb = 0.0
        next_resync = 0.0
        pv_interval = 1.0 / max(self.pv_fps, 1)
        hb_interval = 1.0 / max(self.hb_hz, 1)
        resync_interval = 5.0   # guard against long-run loop drift
        while self._running:
            # apply any pending commands
            try:
                while True:
                    self._apply(self._cmd_q.get_nowait())
            except queue.Empty:
                pass

            if self.stage is None:
                time.sleep(0.05)
                continue

            self.display.make_current()

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
            frame = self.player.get_frame()
            if frame is not None and not self._blackout:
                self.stage.upload_frame(frame)

            if self._blackout:
                gl = self.stage.gl
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
                gl.glViewport(0, 0, self.stage.width, self.stage.height)
                gl.glClearColor(0, 0, 0, 1)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            else:
                self.stage.render_screen()
            self.display.swap_and_flip()

            now = time.monotonic()
            if now >= next_pv:
                next_pv = now + pv_interval
                self._emit_preview()
            if now >= next_hb:
                next_hb = now + hb_interval
                self._emit_heartbeat()
            if now >= next_resync:
                next_resync = now + resync_interval
                self.player.resync_if_needed()

    def _adopt(self, node: str, role: str, ip: str):
        """Adopt an identity assigned from the website: write immersive.conf on
        the boot partition (role + node id + control host) and reboot. The IP
        itself arrives via the control node's DHCP reservation (keyed by this
        node's MAC), so it is recorded here only for reference."""
        if not self.allow_poweroff:
            print(f"[{self.node}] adopt -> {node}/{role} ip={ip} "
                  f"(no-op; system writes disabled on this host)")
            return
        from pathlib import Path
        boot = next((Path(p) for p in ("/boot/firmware", "/boot")
                     if Path(p).is_dir()), Path("/boot"))
        lines = [f"role={role}", f"node={node}",
                 f"control_host={self.control_host}", f"hostname={node}",
                 "allow_poweroff=true"]
        if ip:
            lines.append(f"ip={ip}")
        try:
            (boot / "immersive.conf").write_text("\n".join(lines) + "\n")
            print(f"[{self.node}] adopted {node}/{role}; rebooting to apply")
            import subprocess
            subprocess.Popen(["systemctl", "reboot"])
        except Exception as e:
            print(f"[{self.node}] adopt failed: {e}")

    def _sleep(self, poweroff: bool):
        """Hibernate this node: stop playback, blank the projector, and (on the
        real image) clean-poweroff so the bootloader drops to low-power halt.
        Power is restored externally on wake; the node cold-boots and rejoins."""
        self._scan = None
        self._blackout = True
        self.player.stop()
        if poweroff and self.allow_poweroff:
            import subprocess
            print(f"[{self.node}] hibernate -> systemctl poweroff")
            try:
                subprocess.Popen(["systemctl", "poweroff"])
            except Exception as e:
                print(f"[{self.node}] poweroff failed: {e}")
        else:
            print(f"[{self.node}] hibernate -> blanked (poweroff disabled)")

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

    def _emit_preview(self):
        try:
            rgba = self.stage.render_preview()
            if rgba is None:
                return
            jpg = _jpeg(rgba, self.pv_w, self.pv_h, self.pv_q)
            msg = {"preview": self.node, "jpeg_b64": base64.b64encode(jpg).decode()}
            self._out_q.put_nowait(msg)
        except (queue.Full, Exception):
            pass

    def _emit_heartbeat(self):
        hb = {
            "node": self.node,
            "clock_offset_ns": self.player.clock_offset_ns(),
            "media_pos_ns": self.player.media_pos_ns(),
            "decoder_ok": self.player.decoder_ok(),
            "fb_ok": self.stage is not None,
            "temp_c": _soc_temp_c(),
        }
        try:
            self._out_q.put_nowait(hb)
        except queue.Full:
            pass

    # ---- websocket client (background thread) ---------------------------
    async def _ws_main(self):
        assert websockets is not None, "pip install websockets"
        while self._running:
            try:
                async with websockets.connect(self.ws_url, max_size=4 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({"role": "node", "hello": self.node,
                                              "mac": _node_mac(), "serial": _node_serial()}))
                    print(f"[{self.node}] connected to {self.ws_url}")
                    recv = asyncio.create_task(self._ws_recv(ws))
                    send = asyncio.create_task(self._ws_send(ws))
                    await asyncio.wait([recv, send], return_when=asyncio.FIRST_COMPLETED)
                    recv.cancel(); send.cancel()
            except Exception as e:
                print(f"[{self.node}] ws down ({e}); retry in 2s (still rendering)")
                await asyncio.sleep(2)

    async def _ws_recv(self, ws):
        async for raw in ws:
            try:
                self._cmd_q.put_nowait(json.loads(raw))
            except Exception:
                pass

    async def _ws_send(self, ws):
        loop = asyncio.get_event_loop()
        while True:
            msg = await loop.run_in_executor(None, self._out_q.get)
            await ws.send(json.dumps(msg))

    def start_ws_thread(self):
        def run():
            asyncio.run(self._ws_main())
        threading.Thread(target=run, daemon=True).start()

    # ---- run -------------------------------------------------------------
    def run(self):
        self.start_ws_thread()
        try:
            self.init_display()
        except Exception as e:
            print(f"[{self.node}] display bring-up failed ({e}). "
                  f"Running control-plane only; confirm GLES2-over-GBM on this board.")
        try:
            self.render_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            self.player.stop()
            if self.display:
                self.display.teardown()


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
