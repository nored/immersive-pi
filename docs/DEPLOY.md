# DEPLOY — first install: two nodes, one overlap pair

This is the two-node install that reads the real seam through the real software.
The same binaries run later on all 12 nodes; node count does not change the seam
result, so if a fast pan holds here it holds for the ring; if it steps, a
single-GPU solution is the alternative.

## Hardware

- 3× Raspberry Pi 4 (4 GB). Two are render nodes, one is the control node.
- 1× gigabit switch, dedicated (no other traffic), and short cat6 runs.
- 2× of the room's real projectors, set to **one real overlap pair** (their
  images must physically overlap on the wall by ~8–12 %).
- 3× SD cards, Pi OS **Bookworm** (64-bit), booted to console (no desktop —
  the render node owns DRM/KMS directly).

## Network / static IP plan

Dedicated switch, static IPs, mDNS names. Suggested:

| host   | role          | IP            | mDNS         |
|--------|---------------|---------------|--------------|
| pi-13  | control node  | 10.0.0.13     | pi-13.local  |
| pi-01  | render node   | 10.0.0.1      | pi-01.local  |
| pi-02  | render node   | 10.0.0.2      | pi-02.local  |

Ports: WebSocket control `8765`, net clock `udp/8555`, web UI `8080`.
Where the switch supports it, run `ptp4l` for hardware clock transport; the
GStreamer net clock rides on top regardless.

## Install (each Pi)

```bash
sudo apt install -y gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav \
  python3-gi python3-gst-1.0 libgles2 libegl1 libgbm1 libdrm2 python3-pip
# render nodes only:
pip3 install --break-system-packages -r render-node/requirements.txt
```

Copy the repo to `/opt/immersive` on each Pi. On each render node, copy
`render-node/config.example.json` to `render-node/config.json` and set `node`
(`pi-01` / `pi-02`) and `control_host` (`pi-13.local`).

## Make the worst-case clip (on the control node)

```bash
cd /opt/immersive/test-media && ./make_pan_clip.sh
# copy pan.mp4 to each render node's media_dir (default /opt/immersive/media)
```

A hard white bar + numbered grid panning fast — the worst case for scanout
phase. The numbers let you name exactly which column steps at the seam.

## Start order

**Control node (pi-13)** — one command brings up the clock, the broker, the web
UI, and (with `--autoplay`) starts synced playback once both nodes are up:

```bash
cd /opt/immersive/control-node
python3 controller.py --with-clock --autoplay
```

**Each render node (pi-01, pi-02):**

```bash
cd /opt/immersive/render-node
python3 agent.py --config config.json
# or install the unit: systemctl enable --now render.service
```

That is the single synced-playback path: the controller waits for `pi-01` and
`pi-02` to connect, pushes each its room-model entry, broadcasts `prepare`, then
`play_at` with a base time 300 ms ahead so both arm and fire on the same frame.

Without `--autoplay`, type `play` at the controller prompt to start, `stop` to
halt, `nodes` to list who is connected.

## Reading the seam (the test, in one sentence)

Stand at the overlap, watch the white bar cross it, and decide whether it stays
one continuous line or steps as it crosses.

- **Holds** → scanout phase is tolerable; scale the same software to 12.
- **Steps** → the free-running HDMI phase is visible on fast motion; no software
  fixes it, and a single-GPU solution is the alternative.

## What the heartbeats tell you

The controller prints one line per node per second:

```
[hb] pi-01  off=  +0.412ms pos=  3.300s dec=ok fb=ok 48.7C
```

- `off` — net-clock offset from master. Tens of µs to low ms is normal and is
  *media* alignment, not scanout phase. It does **not** predict the seam; only
  the wall does.
- `pos` — media position; the two nodes should track within a frame.
- `dec/fb` — decoder and framebuffer health. `temp` — throttle watch (Pi 4
  throttles ~80 °C).

## Performance note (Pi 4, 1080p60)

The decode path uses `videoconvert` to RGBA for the simplest correct GL upload.
If the Pi 4 can't hold 60 fps at 1080p, keep NV12 out of `v4l2h264dec` and do
YUV→RGB in the fragment shader (one luma + one chroma texture) to take the
convert off the CPU. The warp/blend math is unchanged.

## Codec / board decision (surfaced, not guessed)

Milestone 1 assumes **H.264 + Pi 4** because that keeps the Pi 4 hardware decoder
for 12 always-on nodes (`v4l2h264dec`). If content must be H.265 the nodes move
to Pi 5 (HEVC decoder, no H.264 block). Confirm GLES2-over-GBM/DRM headless on
the chosen board here before ordering nodes beyond this pair.
