# DEPLOY

Deployment is the Yocto image — there is nothing to install on a node. The image
contains the full runtime (GStreamer, GLES2/mesa, Python, the apps). Everything
after flashing is done from the boot config file or the website.

## Per node

1. Flash the image (`immersive-pi-<version>.img.xz`) to an SD card with
   Raspberry Pi Imager (or any SD-card imager).

2. Open the boot partition (it mounts like a USB drive) and edit the
   `immersive.conf` text file:

   ```
   role=render            # render | control
   node=pi-01             # this node's id (the control node could be e.g. pi-13)
   control_host=pi-13.local
   hostname=pi-01
   ```

3. Boot. The node takes DHCP, becomes reachable as `<hostname>.local`, and starts
   its role.

`immersive.conf` lives on the boot partition (survives an OTA), and the same
settings can be changed from each node's admin page (`http://<node>.local:8080/admin`).

## Network

DHCP + mDNS. Each node takes the network's DHCP for its address and is reachable
as `<node>.local`. Render nodes reach the control node by its `.local` name
(`control_host`); the fleet appears in the website's node list. No static IPs are
assigned. See `ENROLLMENT.md`.

## Running the show

Everything is on the control node's website at `http://<control>.local:8080`:
upload or select a video and Play/Stop, calibrate, auto-calibrate, sleep/wake.
No commands to run.

## Reading the seam

Nodes share a media clock but not HDMI scanout phase, so a seam can show
sub-frame tearing on fast motion. Play the pan clip and watch a vertical bar
cross an overlap: if it stays continuous the scanout phase is tolerable; if it
steps, a single-GPU solution is the alternative.

## Heartbeats

The dashboard (`http://<control>.local:8080/dashboard.html`) shows one card per
node at 1 Hz: clock offset, media position, decoder/framebuffer state, and SoC
temperature, flagging drift, stall, or thermal throttle.

## Codec

The decode path targets H.264 (`v4l2h264dec`).
