# meta-immersive — Yocto image for the 360 room

A scarthgap Yocto image for the immersive Pi cluster. **One image serves both
roles** — each node reads `role=render|control` from `immersive.conf` on the boot
FAT — with **A/B partitions and over-the-air updates**. This is the OS for the
nodes; this README is for *building* the image (a developer task). Operators only
flash it and use the website — see `docs/DEPLOY.md`.

## What the image is

- **Render role:** the GStreamer→GLES2 warp/blend→KMS stage on bare DRM/GBM (no
  compositor), slaved to the control node's net clock.
- **Control role:** clock master, controller, calibration website, and the
  Node-RED power API. Renders no video.
- **A/B + OTA:** two rootfs slots; a nightly timer pulls the latest encrypted
  GitHub release, flashes the inactive slot, switches `cmdline.txt`, and reboots;
  userspace health confirmation can restore the previous root slot. The kernel
  and firmware on the boot FAT are shared: an unbootable kernel is outside this
  userspace rollback check. Device configuration, calibration history, media and
  SSH credentials are carried into the new root slot.
- **Boot provisioning:** a deploy recipe places `immersive.conf` on the actual
  WIC boot FAT. Firmware updates preserve the card's existing configuration
  instead of replacing it with the factory template.
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

`build.sh` vendors `render-node/`, `control-node/` and `shared/` using the same
runtime manifest as deployment. It excludes concrete node configuration,
calibration and uploaded media. The Dockerfile pins all three Yocto layer
revisions. PyOpenGL 3.1.10 includes the EGL/GLES2 bindings used by the renderer;
Linux CMA is explicitly 512 MB, matching the deployed fleet.

Set an explicit candidate version when needed:

```bash
IMMERSIVE_VERSION=pi-v1.0.8 ./build.sh
```

The version file changes only after a successful build. Completed artifacts live
under `output/<version>/`, with compatibility symlinks in `output/`. Previous
artifacts are retained if a build fails or a version is rebuilt:

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

The boot-health check accepts an idle renderer, but requires its framebuffer,
local admin API, expected image version and H.264 hardware-decoder factory.
A successful build still needs a spare-card boot and projector test before an OS
release is published; runtime updates do not require reflashing the fleet.

Inspect completed artifacts without mounting them. Set `EXPECTED_RUNTIME_RELEASE`
to the release identifier produced from the current source by
`tools/runtime_release.py build`, rather than reading it from the image itself:

```bash
# Run from the repository root; the builder provides read-only debugfs access.
docker run --rm --entrypoint python3 -v "$PWD:/workspace:ro" -w /workspace \
  immersive-yocto tools/verify_image.py \
  --rootfs yocto/output/pi-v1.0.8/immersive-rootfs.ext4 \
  --boot yocto/output/pi-v1.0.8/immersive-boot.tar \
  --release "$EXPECTED_RUNTIME_RELEASE" --version pi-v1.0.8 \
  --public-key yocto/secrets/authorized_keys
```

This checks runtime file hashes, version, ARM64 decoder plugins, PyOpenGL,
operator SSH keys and permissions, PoE configuration, and the 512 MB CMA boot
argument. It also rejects embedded development calibration and node identity.
It does not replace the spare-card hardware test.

`tools/verify_flash_image.py` checks the full `.img`, including its partition
bounds, boot FAT, A-slot selector and installed runtime. Run it in the same
read-only builder container with `--image`, `--release`, `--version` and
`--public-key` arguments.

On an ARM64 Linux host, `tools/smoke_image.py` executes the image's Python/GLES
bindings and software decoder, checks root public-key authentication and rejects
anonymous login, and exercises the installed partition-table template against a
disposable file. Run this tool with `--user 0` in the builder container, passing
`--rootfs` and a native H.264 test clip via `--media`. It extracts into temporary
container storage and leaves the input artifacts unchanged.

First-boot resize preserves the MBR identifier. It records a pending resize
before changing the table and checks the kernel's partition offsets before
growing A or formatting B. If those offsets are stale, it reboots and resumes
before touching either filesystem. Verify that path on the spare card as well.
