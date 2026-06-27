# immersive-pi

Drive a multi-projector display from a Raspberry Pi cluster: a set of render
nodes (one projector each) plus a control node. Each render node warps,
edge-blends, and colour-corrects flat source video in real time and scans it out
to its projector; the control node keeps the nodes on a shared media clock and
serves a calibration tool.

The projection mapping is live data, not pre-baked pixels. Each node recomputes
its warp + blend + colour from a room-model the control node pushes, so the
installation can be recalibrated — and a single swapped projector re-calibrated —
without touching the other nodes.

## What it does

- **Real-time warp / blend / colour** — per node, an NxM control-point mesh
  (source UV → output clip XY), per-edge soft-edge blending (width, gamma,
  black-level lift), and per-node colour trim (gain, gamma, lift), on GLES2.
- **Synchronised playback** — a network clock holds the nodes on the same media
  frame; a shared loop epoch keeps long runs aligned.
- **Calibration website** — preview grid, draggable mesh handles over each node's
  live preview, test patterns, blend and colour editors, git-versioned save, and
  a single-projector recalibration flow.
- **Structured-light auto-calibration** — a gray-code scan from one camera solves
  the meshes and blend masks.
- **Operations** — heartbeat dashboard (drift / stall / thermal); nodes keep
  looping if the control node restarts; add / enrol nodes from the website.
- **Sleep / wake** — an HTTP endpoint (e.g. for Node-RED) powers the display down
  and back up via a pluggable backend (PoE / PDU / GPIO).
- **Appliance image** — a Yocto build with A/B partitions and OTA updates; one
  image serves either role, selected by a boot-partition config.

## Architecture

```
Render node                                Control node
─────────────                              ────────────
GStreamer v4l2 HW decode  ── net clock ──  clock_master.py   (net time provider)
  → GL texture                             controller.py     (WebSocket broker,
  → NxM mesh warp (warp.vert)                                 show control, heartbeats,
  → blend + colour (blend.frag)                               power + media API)
  → KMS/DRM scanout → projector            roommodel.py      (git-versioned model,
  → preview FBO → JPEG → WebSocket ──────►                    per-node push)
                                           web/              (calibration site)
```

## Layout

```
render-node/    player.py  gl_pipeline.py  drm_kms.py  agent.py  shaders/  systemd/
control-node/   clock_master.py  controller.py  roommodel.py  powerctl.py
                autocalib/  web/  room-model.json
test-media/     make_pan_clip.sh
yocto/          meta-immersive    # appliance image (A/B + OTA)
docs/           DEPLOY  FLEET  AUTOCALIB  HIBERNATION  ENROLLMENT
```

## Running

Deployment is the Yocto image (flash, set role + id in `immersive.conf` per node,
boot) — see `yocto/README.md` and `docs/DEPLOY.md`.

From a source checkout, for development:

```bash
cd control-node && python3 controller.py --with-clock --autoplay   # control node
cd render-node  && python3 agent.py --config config.json           # render node
```

## room-model

Canonical on the control node, git-versioned (`control-node/room-model.json`).
Each node receives only its own entry. One primitive — an NxM control-point mesh
(UV → clip XY) — covers a flat wall (2×2 corner-pin) through curved surfaces
(subdivide to 4×4 / 8×8); blend is a per-edge layer and colour a per-node layer.

## Notes

- **Scanout phase:** nodes share a media clock but not HDMI scanout phase, so a
  seam can show sub-frame tearing on fast motion. The supplied pan clip
  (`test-media/make_pan_clip.sh`) is for checking this on the wall.
- **Codec:** the decode path targets H.264 (`v4l2h264dec`).
