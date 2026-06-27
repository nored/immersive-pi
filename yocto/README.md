# meta-immersive — Yocto image for the 360 room

A scarthgap Yocto image for the immersive 360 Pi cluster. **One image serves both
roles** — each node reads `role=render|control` from `immersive.conf` on the boot
FAT — with **A/B partitions and encrypted over-the-air updates**, the same
pattern as yBrowser. This is the production OS for the nodes; it supersedes the
Ansible/Pi-OS path in `provision/ansible` (kept as an alternative).

## What the image is

- **Render role:** the GStreamer→GLES2 warp/blend→KMS stage on bare DRM/GBM (no
  compositor), slaved to the control node's net clock.
- **Control role:** clock master, controller, calibration website, and the
  Node-RED power API. Renders no video.
- **A/B + OTA:** two rootfs slots; a nightly timer pulls the latest encrypted
  GitHub release, flashes the inactive slot, switches `cmdline.txt`, and reboots;
  a failed boot rolls back. Boot-FAT `immersive.conf` survives the rootfs rewrite.
- **Config without reflashing:** edit `immersive.conf` on the boot partition to
  set a node's role, id, control host, and (control) the Node-RED API token.
- Silent boot, key-only dropbear SSH, mDNS `<hostname>.local` via avahi.

## Build

Docker-based, no host deps beyond Docker:

```bash
cd yocto
head -c 32 /dev/urandom > secrets/update.key      # once; back it up
cp ~/.ssh/id_ed25519.pub secrets/authorized_keys  # your login key
./build.sh
```

`build.sh` vendors `render-node/` + `control-node/` into the layer, bumps
`pi-vX.Y.Z`, and produces in `output/`:

| Artifact | Purpose |
|---|---|
| `immersive-image-immersive-pi.img` | seed image for SD cards |
| `immersive-rootfs.ext4` | shrunk rootfs for OTA bundles |
| `immersive-boot.tar` | boot partition contents for OTA bundles |

Flash a seed card, then set the node's identity on the boot partition:

```bash
sudo dd if=output/immersive-image-immersive-pi.img of=/dev/sdX bs=4M conv=fsync
# mount the boot FAT and edit immersive.conf:  role=render  node=pi-01  control_host=pi-13.local
```

## Publish an OTA update

```bash
./build.sh && ./release.sh        # encrypts rootfs+boot, gh release on $REPO
```

Nodes pick it up on the nightly timer, or immediately:

```bash
ssh root@pi-07.local systemctl start immersive-updater
```

## immersive.conf keys

| key | role | meaning |
|---|---|---|
| `role` | both | `render` or `control` |
| `node` | both | node id, e.g. `pi-01` |
| `control_host` | render | where the clock + controller live |
| `hostname` | both | mDNS `<hostname>.local` |
| `api_token` | control | token Node-RED sends to `/api/hibernate`/`/api/wake` |
| `allow_poweroff` | render | let hibernate power the Pi off (default true) |
| `timezone` | both | e.g. `Europe/Berlin` |

## Layout

```
conf/machine/immersive-pi.conf     raspberrypi4-64 + vc4-kms-v3d, silent boot
recipes-core/images/immersive-image.bb   one image, both roles, GStreamer+mesa+python+opencv
recipes-immersive/immersive        app -> /opt/immersive, role dispatch + systemd units
recipes-immersive/immersive-updater A/B OTA (updater, confirm-or-rollback, first-boot resize)
recipes-immersive/immersive-update-key   the baked AES/HMAC key (from secrets/)
recipes-bsp/bootfiles/rpi-cmdline.bbappend   silent-boot cmdline
wic/immersive-ab.wks.in            boot + rootfsA + rootfsB + data
Dockerfile build.sh release.sh VERSION   Docker build + encrypted release tooling
```
