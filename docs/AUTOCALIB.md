# AUTOCALIB — structured-light auto-calibration

Solve the whole room-model automatically: each projector shows a gray-code
sequence, one room camera captures it, the control node decodes per-pixel
projector↔camera correspondence and solves all meshes, the ring closure, and the
blend masks in one pass. The result is the same room-model the calibration tool
edits, so manual touch-up from Milestone 2 stays valid on top — and a swapped
beamer becomes a re-scan instead of a hand edit.

## Hardware

- One ordinary USB or Pi camera on the control node, positioned to see the whole
  ring (or enough of it). Lock its exposure/white-balance if you can.
- The render nodes and controller already running (Milestones 1–3).

## Install

```bash
pip3 install --break-system-packages -r control-node/requirements.txt   # adds opencv
```

## Run

```bash
cd control-node/autocalib
python3 scan.py --camera 0 --proj 1920x1080 --controller pi-13.local
#   --strip        open strip instead of a closed 360 ring
#   --cols/--rows  solved mesh resolution per node (default 4x4)
#   --nodes pi-01 pi-02 ...   explicit subset/order (e.g. one swapped node)
```

What it does, per projector (others blacked out so the camera attributes light
to one source):

1. projects the gray-code stack — references (white/black) then X bitplanes then
   Y bitplanes, each with its inverse for robust per-pixel thresholding;
2. captures each plane with the camera;
3. decodes to `proj_x / proj_y / mask` in camera space.

Then `solve.py` bins camera pixels onto each node's output grid, normalises the
camera coordinate over the union of all footprints to get source UV, and reads
blend widths from the camera-space overlaps between neighbours. Geometry is
pushed to the nodes through the controller and the model is saved + git-committed
exactly like a manual save.

## Beamer-swap re-scan

A replaced projector is just a one-node scan:

```bash
python3 scan.py --camera 0 --nodes pi-07 --controller pi-13.local
```

Only `pi-07`'s entry changes; the other eleven are untouched. (For a clean
single-node solve, keep its two neighbours lit during its scan if you want the
overlaps re-measured, or scan the trio `pi-06 pi-07 pi-08`.)

## How it's verified

The geometry math runs and self-tests with no camera:

```bash
python3 control-node/autocalib/graycode.py   # decode is pixel-exact through albedo+ambient
python3 control-node/autocalib/solve.py       # meshes ordered, overlaps -> blend widths
python3 control-node/autocalib/scan.py --self-test   # synth -> decode -> solve -> model+commit
```

The node-side pattern generator (`render-node/scanpattern.py`) is byte-for-byte
identical to the control-node decoder's reference patterns, so what the projector
shows is exactly what the decoder expects.

## Limits / extensions

- One camera that can't see the full ring → capture in arcs and stitch on shared
  projectors; the solver already places everything in one camera frame, so
  extending to multi-capture is a coordinate-stitch step before `solve()`.
- Bezier-smoothed mesh interior (a later refinement on the same mesh) and
  photometric (color) auto-solve are not done here; color stays manual.
