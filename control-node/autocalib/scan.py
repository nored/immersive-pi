"""scan.py — structured-light auto-calibration orchestrator.

Runs the whole pass from the control node:

  for each projector (others black):
      project the gray-code stack one bitplane at a time
      capture each with the single room camera
      decode -> projector↔camera correspondence
  solve all meshes + ring closure + blend masks in one pass
  push the result through the controller (set_mesh/set_blend/...) and save+commit

The camera is one ordinary USB/Pi camera that can see the whole ring (or enough
of it). Manual edits from the calibration website remain valid on top — this just
gives them a solved starting point, and turns a swapped beamer into a re-scan.

Hardware deps (control node only): opencv-python, websockets. The decode and
solve are pure numpy and are covered by graycode.py / solve.py self-tests; this
file's own `--self-test` exercises the synthesize→decode→solve→write path with
no camera or network.

    python3 scan.py --camera 0 --proj 1920x1080 --controller pi-13.local
    python3 scan.py --self-test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # control-node/

import graycode
import solve as solver
from roommodel import RoomModel


# ---- camera ---------------------------------------------------------------
class Camera:
    def __init__(self, index: int, warmup: int = 5, settle_s: float = 0.25):
        import cv2
        self.cv2 = cv2
        self.cap = cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise RuntimeError(f"camera {index} did not open")
        self.warmup = warmup
        self.settle_s = settle_s

    def grab_gray(self) -> np.ndarray:
        # flush stale frames so we capture the pattern currently on the wall
        time.sleep(self.settle_s)
        for _ in range(self.warmup):
            self.cap.read()
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError("camera read failed")
        return self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2GRAY).astype(np.float32)

    def close(self):
        self.cap.release()


# ---- controller link ------------------------------------------------------
class Link:
    """Thin web-client to the controller: send commands, read the snapshot."""

    def __init__(self, host: str, port: int = 8765):
        import websockets
        self._ws_mod = websockets
        self.url = f"ws://{host}:{port}"
        self.ws = None
        self.nodes: list[str] = []

    async def __aenter__(self):
        self.ws = await self._ws_mod.connect(self.url, max_size=4 * 1024 * 1024)
        await self.ws.send(json.dumps({"role": "web", "hello": "scanner"}))
        snap = json.loads(await self.ws.recv())
        self.nodes = [n["node"] for n in snap.get("room", {}).get("nodes", [])]
        return self

    async def __aexit__(self, *a):
        if self.ws:
            await self.ws.close()

    async def send(self, obj):
        await self.ws.send(json.dumps(obj))


# ---- the pass -------------------------------------------------------------
async def run(args):
    PW, PH = (int(x) for x in args.proj.lower().split("x"))
    cam = Camera(args.camera)
    scans = {}
    cam_shape = None
    async with Link(args.controller, args.ws_port) as link:
        nodes = args.nodes or link.nodes
        if not nodes:
            raise SystemExit("no nodes to scan")
        print(f"scanning {len(nodes)} nodes at projector {PW}x{PH}")
        for node in nodes:
            # everyone else dark so the camera attributes light to one projector
            for other in nodes:
                if other != node:
                    await link.send({"cmd": "blackout", "node": other, "on": True})
            captures = {}
            for name, axis, bit, inv in graycode.scan_sequence(PW, PH):
                await link.send({"cmd": "scan", "node": node, "axis": axis,
                                 "bit": bit, "inverted": inv, "on": True})
                gray = cam.grab_gray()
                captures[name] = gray
                cam_shape = gray.shape
            await link.send({"cmd": "scan", "node": node, "on": False})
            for other in nodes:
                if other != node:
                    await link.send({"cmd": "blackout", "node": other, "on": False})
            px, py, mask = graycode.decode(captures, PW, PH)
            scans[node] = (px, py, mask)
            print(f"  {node}: coverage {mask.mean()*100:.1f}%")

        entries = solver.solve(scans, cam_shape, PW, PH,
                               cols=args.cols, rows=args.rows, ring=not args.strip,
                               order=nodes)
        await push_and_save(link, entries)
    cam.close()
    print("auto-calibration complete; refine on the wall in the calibration tool.")


async def push_and_save(link, entries):
    """Push solved geometry to nodes via the controller, then save+commit."""
    for e in entries:
        await link.send({"cmd": "set_source_region", "node": e["node"],
                         "source_region": e["source_region"]})
        await link.send({"cmd": "set_mesh", "node": e["node"], "mesh": e["mesh"]})
        await link.send({"cmd": "set_blend", "node": e["node"], "blend": e["blend"]})
    await link.send({"cmd": "save", "message": "structured-light auto-calibration"})


# ---- self-test: synthesize -> decode -> solve -> write model (no hardware) -
def _self_test():
    import tempfile, shutil, os
    PW, PH = 256, 128
    CW, CH = 600, 120
    N = 3
    overlap = 0.10
    seg = CW / (N - overlap * (N - 1))
    nodes = [f"pi-{i+1:02d}" for i in range(N)]

    scans = {}
    for i, node in enumerate(nodes):
        cam_x0 = i * (seg * (1 - overlap))
        cam_x1 = cam_x0 + seg
        cy, cx = np.mgrid[0:CH, 0:CW]
        inside = (cx >= cam_x0) & (cx < min(cam_x1, CW))
        true_px = np.clip((cx - cam_x0) / max(cam_x1 - cam_x0, 1) * (PW - 1), 0, PW - 1)
        true_py = (cy / (CH - 1) * (PH - 1))
        captures = {}
        for name, axis, bit, inv in graycode.scan_sequence(PW, PH):
            proj = (np.full((PH, PW), 0 if inv else 255, np.uint8) if axis is None
                    else graycode.make_pattern(axis, bit, inv, PW, PH)[:, :, 0])
            seen = np.zeros((CH, CW), np.float32)
            ipy = true_py.astype(int); ipx = true_px.astype(int)
            seen[inside] = proj[ipy[inside], ipx[inside]]
            captures[name] = seen
        px, py, mask = graycode.decode(captures, PW, PH)
        mask &= inside
        scans[node] = (px, py, mask)

    entries = solver.solve(scans, (CH, CW), PW, PH, cols=4, rows=4, ring=False, order=nodes)

    # write into a throwaway model + commit, exactly like push_and_save's effect
    tmp = tempfile.mkdtemp()
    model = {"version": 0, "show": "x", "nodes": [
        {"node": n, "projector": f"P{i+1}", "source_region": {}, "mesh": {},
         "blend": {}, "color": {"gain": [1, 1, 1], "gamma": 2.2, "lift": [0, 0, 0]}}
        for i, n in enumerate(nodes)]}
    p = Path(tmp) / "room-model.json"
    p.write_text(json.dumps(model))
    os.system(f"git -C {tmp} init -q && git -C {tmp} add -A && "
              f"git -C {tmp} -c user.email=t@t -c user.name=t commit -qm init")
    rm = RoomModel(p)
    solver.write_into_model(rm.model, entries)
    rm.save("structured-light auto-calibration")
    reloaded = json.loads(p.read_text())
    assert reloaded["version"] == 1
    assert all(len(n["mesh"]["points"]) == 16 for n in reloaded["nodes"])
    assert reloaded["nodes"][1]["blend"]["left"]["width"] > 0.05
    commits = os.popen(f"git -C {tmp} log --oneline").read().strip().splitlines()
    print(f"scan self-test OK — solved {len(entries)} nodes, written to model "
          f"v{reloaded['version']}, {len(commits)} commits, color preserved")
    shutil.rmtree(tmp)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--proj", default="1920x1080", help="projector resolution WxH")
    ap.add_argument("--controller", default="pi-13.local")
    ap.add_argument("--ws-port", type=int, default=8765)
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--rows", type=int, default=4)
    ap.add_argument("--strip", action="store_true", help="open strip, not a ring")
    ap.add_argument("--nodes", nargs="*", help="explicit node order (default: from controller)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        _self_test()
        return 0
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
