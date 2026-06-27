# AUTOCALIB — structured-light auto-calibration

Calibrates the projection automatically from the website. Each projector flashes
a gray-code sequence, a camera on the control node scans them, and the control
node solves the per-node meshes and blend masks and applies them.

## Requirements

- A USB or Pi camera connected to the control node, positioned to see the
  projection. Fixed exposure / white-balance gives more stable results.
- The render nodes connected (they appear in the website's node list).

## Run it

On the calibration website, click **Auto-calibrate**. Projectors flash the
patterns one at a time, the camera scans, and progress shows in the header. When
it finishes, the solved geometry and blends are applied and saved.

Refine any node by hand in the editor afterwards — the auto-solve is a starting
point, not a lock.

## Swapped projector

Re-run **Auto-calibrate** (or the beamer-swap flow) for the replaced node; the
other nodes are untouched.
