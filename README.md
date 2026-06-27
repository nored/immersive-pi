# immersive-pi

Drive a multi-projector immersive/360° display from a Raspberry Pi cluster: up to
12 render nodes (one projector each) plus a control node. Each render node warps,
edge-blends, and colour-corrects flat source video in real time and scans it out
to its projector; the control node keeps all nodes on a shared media clock and
serves a calibration tool.

The projection mapping is **live data**, not pre-baked pixels. Every node
recomputes its warp + blend + colour from a room-model the control node pushes,
so the installation can be recalibrated — and a single swapped projector
re-calibrated — without touching the other nodes.

## What it does

- **Real-time warp/blend/colour** — per node, an NxM control-point mesh (source
  UV → output clip XY) plus per-edge soft-edge blending (width, gamma,
  black-level lift) and per-node colour trim (gain, gamma, lift), all on GLES2.
- **Synchronised playback** — a network clock holds every node on the same media
  frame; a shared loop epoch keeps long runs from drifting apart.
- **In-room calibration website** — a tablet tool: live preview grid, draggable
  mesh handles over each node's preview, test patterns, blend and colour editors,
  git-versioned save, and a single-projector recalibration flow.
- **Structured-light auto-calibration** — a gray-code scan from one camera on the
  control node solves the meshes, ring closure, and blend masks automatically.
- **Fleet operations** — heartbeat dashboard (drift / stall / thermal),
  master-reboot resilience (nodes keep looping without the control node).
- **Sleep/wake** — an HTTP endpoint (e.g. for Node-RED) powers the display down
  and back up via a pluggable backend (PoE / PDU / GPIO).
- **Appliance image** — a Yocto build with A/B partitions and encrypted OTA
  updates; one image serves either role, selected by a boot-partition config.

## Architecture

```
Render node (×N)                           Control node (×1)
─────────────────                          ────────────────────────
GStreamer v4l2 HW decode  ── net clock ──  clock_master.py   (net time provider)
  → GL texture                             controller.py     (WebSocket broker,
  → NxM mesh warp (warp.vert)                                 show control, heartbeats,
  → blend + colour (blend.frag)                               power API)
  → KMS/DRM scanout → 1 projector          roommodel.py      (git-versioned model,
  → preview FBO → JPEG → WebSocket ──────►                    per-node push)
                                           web/              (calibration site)
```

## Layout

```
render-node/    player.py  gl_pipeline.py  drm_kms.py  agent.py  shaders/  systemd/
control-node/   clock_master.py  controller.py  roommodel.py  powerctl.py
                autocalib/  web/  room-model.json
test-media/     make_pan_clip.sh
provision/      ansible/          # Pi-OS provisioning (alternative to the image)
yocto/          meta-immersive    # appliance image (A/B + OTA)
docs/           DEPLOY  FLEET  AUTOCALIB  HARDENING  HIBERNATION
```

## Quick start (from a checkout)

```bash
# control node
cd control-node && python3 controller.py --with-clock --autoplay
# each render node
cd render-node && python3 agent.py --config config.json
```

See `docs/DEPLOY.md` for wiring, static IPs, and the test clip. For the appliance
image (flash + boot-config per node) see `yocto/README.md`.

## room-model

Canonical on the control node, git-versioned (`control-node/room-model.json`).
Each node receives only its own entry. One primitive — an NxM control-point mesh
(UV → clip XY) — covers flat walls (2×2 corner-pin) through curved surfaces
(subdivide to 4×4 / 8×8); blend is a per-edge layer and colour a per-node layer.

## Notes

- **Scanout phase:** independent Pis share a media clock but not HDMI scanout
  phase, so a seam can show sub-frame tearing on fast motion. Validate on the
  wall with the supplied pan clip before scaling out (`docs/DEPLOY.md`).
- **Codec / board:** the decode path targets H.264 on Pi 4 (`v4l2h264dec`); H.265
  content implies Pi 5. The GL stage runs on both.
