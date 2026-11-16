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
- **Synchronised playback** — numbered frames, a shared network clock and an
  all-node commit barrier coordinate decoding and presentation. Independent HDMI
  scanout phase and occasional deadline failures remain under investigation.
- **Calibration website** — node health, draggable mesh handles over a static
  alignment grid, projector test patterns, blend and colour editors, git-versioned save, and
  a single-projector recalibration flow.
- **Experimental phone calibration** — an iPhone records camera, LiDAR and pose
  measurements with live guidance. Legacy automatic calibration is retired;
  automatic scan completion and production calibration are still unfinished.
- **Operations** — heartbeat dashboard (drift / stall / thermal); discover / register / enrol nodes from the
  website; per-node admin page (health + reconfigure + reboot) with a login.
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
                                                              per-node push)
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

Blend gamma describes the projector's light response: an overlap weight `t`
uses the encoded multiplier `t^(1/gamma)`, so two complementary masks add to
constant light. Colour-trim gamma defaults to `1` because video pixels are
already encoded. Existing saved colour settings are retained. Blending still
requires matching source content in the physical overlap and measured geometry;
fading unrelated tile edges cannot align the image.

The **Source / overlap** panel edits one node's panorama window in percent
(8.333% spans 30°), with explicit wrapping across the 360° seam. During a
prepared or playing tiled show, incompatible source/UV edits are rejected
before changing the model or projector. Playback also refuses a tile package
that lacks pixels required by the configured source windows.

To plan wider coverage, stop playback, edit the measured source windows, and
use **Download room model**. Generate matching clips with
`python3 test-media/make_native_meteor.py --room-model room-model.json --outdir new-tiles`.
Upload the clips and their manifest, select that manifest, and test alignment
on the wall. **Save + commit** persists accepted edits; applying or downloading
a source draft does not automatically save it. The generator's `--overlap`
uses a fraction of each projector's output width, rather than panorama width.

## Notes

### Panoramic diagnostic video

Generate a numbered panorama with an arrow moving clockwise around the room:

```bash
python3 test-media/make_ring_clip.py --nodes 12 --out test-media/ring-test-12.mp4
```

Requires Python Pillow and `ffmpeg`. The default is 48 seconds of 1920×1080,
30 fps H.264: two 24-second revolutions. Change `--nodes` for a different number
of sectors; use a matching output filename to avoid the render nodes' existing
media cache. Upload the MP4 through the control website and select it for playback.

Sector numbers mark panorama positions, not physical node identities. Each Pi
downloads and decodes the same complete video, then displays its configured
`source_region`. The clip does not change calibration or automatically distribute
the panorama: the room model must already assign the appropriate source regions.
The checked-in model is a two-projector example, despite its `canvas.cols=12`.

Walk the room to check ascending sector order, missing sections, continuous grid
lines, and the arrow crossing projector boundaries. Compare repeated frame/time
counters on adjacent projectors to spot playback offsets. The last-to-first seam
also tests panorama wrapping. Shared media clocks do not provide HDMI genlock.

- **Scanout phase:** nodes share a media clock but not HDMI scanout phase, so a
  seam can show sub-frame tearing on fast motion. The supplied pan clip
  (`test-media/make_pan_clip.sh`) is for checking this on the wall.
- **Codec:** the decode path targets H.264 (`v4l2h264dec`).

### Dortmund room devices

The controller proxies Node-RED from the same UI; browsers need only the control
node URL. Add this block to the existing room-model JSON (preserve its nodes and
calibration):

```json
"room_devices": {
  "base_url": "http://192.168.230.156:1880",
  "ready_timeout_s": 180
}
```

Without this block, projector integration is disabled. The controller polls
`/projector/dortmund/status` every three seconds. The UI distinguishes actual
state (`ist`), requested target (`ziel`), reachability and errors. Failed reads
retain the previous values marked stale; they cannot satisfy playback readiness.

**Start / Play** requests presentation off and panorama on, then waits for all
12 panorama projectors to report `ist=an`, `antwortet=true`, and no error before
starting video. A successful webhook response alone never counts as readiness.
**Stop** stops video and leaves the projectors on. **Sleep** stops video, requests
both projector groups off, and puts render nodes into their configured standby;
the existing Pi/PoE power backend remains separate. **Wake** powers up panorama
and wakes the render nodes; select Start / Play to begin playback. Individual
projectors, presentation mode and both curtains are available under Room devices.
Curtains move only through their explicit controls. Switching projector modes
stops the current show first. No toggle commands are used, so retries cannot
accidentally invert the requested power state.

The Node-RED API currently reports power, not HDMI input lock or optical output.
Readiness therefore does not prove that the projector is displaying the video.

Live video previews are intentionally absent: render nodes do no preview GPU
readback, JPEG encoding, or image streaming. The browser shows projector state,
node health and frame progress; calibration uses the local mesh editor and
patterns on the actual projectors. Legacy image messages are discarded.

## Room media and current validation

The Dortmund installation uses twelve identical **1280×800** outputs. The five
downloaded real 360° films cover underwater kelp/fish, sea lions, a snowy-city
drone flight, mountain paragliding and an Alaska glacier flight. Credits and
download provenance live in `test-media/media-library/catalog.json`. See
[`tools/media-library/README.md`](tools/media-library/README.md) for conversion
and publication. Source movies, phone recordings and generated clips stay local.

The current test movies bake experimental recovered phone-scan geometry and
provisional blend weights into their pixels. They do not install a validated
room calibration. The saved room model remains at its existing baseline.
ARKit pose jumps were recovered using common QR anchors; held-out residuals
still exceed the production target. Automatic scan completion, global seam
alignment and measured brightness blending remain open work.

On the M3, a short accelerated conversion reached about 25 scene frames per
second across all twelve views, versus about 8.5 for the earlier full render.
Full-length throughput varies with source decoding and projection conversion.
VideoToolbox SPS timing repair copies compressed pictures without re-encoding;
the H.264 encoding itself is lossy. Native player preparation validates the
published clips' dimensions, frame rate, picture count and hashes.

The deployed runtime release is `5e43a664e9239851`. The locally built Yocto image
predates that runtime and still needs an updated build and physical boot test.
Long playback runs have produced deadline/skew failures; short successful
playtests do not establish production frame-perfect optical synchronisation.
