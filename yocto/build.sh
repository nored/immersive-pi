#!/bin/bash
# Build the immersive-pi Yocto image in Docker.
# Output: output/immersive-image-immersive-pi.img (+ rootfs.ext4 + boot.tar for OTA)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "Building immersive-pi Yocto image..."
echo "First run takes a while (1-3 hours)."
echo ""

mkdir -p output
echo "Removing previous build artifacts..."
rm -fv output/immersive-image-immersive-pi.img \
       output/immersive-rootfs.ext4 \
       output/immersive-boot.tar
echo ""

# Vendor the app into the layer so the Docker context (yocto/) carries it. The
# image recipe installs render-node/ + control-node/ from here.
echo "Staging app into the layer..."
APPDST="$SCRIPT_DIR/recipes-immersive/immersive/files/app"
rm -rf "$APPDST"
mkdir -p "$APPDST"
for d in render-node control-node; do
    rsync -a --delete \
        --exclude '__pycache__' --exclude '*.pyc' \
        --exclude 'config.json' --exclude '*.mp4' \
        "$REPO_ROOT/$d" "$APPDST/"
done
echo "  staged: $(du -sh "$APPDST" | cut -f1)"
echo ""

# Bump version: pi-vMAJOR.MINOR.PATCH, patch++ (seeded from VERSION).
VERSION_FILE="$SCRIPT_DIR/VERSION"
CUR_VER=$(cat "$VERSION_FILE" 2>/dev/null || echo "pi-v1.0.0")
_b=${CUR_VER#pi-v}; _maj=${_b%%.*}; _r=${_b#*.}; _min=${_r%%.*}; _pat=${_r#*.}
IMMERSIVE_VERSION="pi-v${_maj}.${_min}.$((_pat + 1))"
echo "$IMMERSIVE_VERSION" > "$VERSION_FILE"
export IMMERSIVE_VERSION
echo "Build version: $IMMERSIVE_VERSION"
echo ""

docker build -t immersive-yocto .
docker run --rm \
    -e IMMERSIVE_VERSION="$IMMERSIVE_VERSION" \
    -v immersive-yocto-cache:/home/builder/build \
    -v "$(pwd)/output":/output \
    immersive-yocto \
    bash -c '
        cd /home/builder
        # the named cache volume mounts as root on first run; make it writable
        sudo chown builder:builder /home/builder/build /output 2>/dev/null || true
        source poky/oe-init-build-env build
        cp /home/builder/build-conf/bblayers.conf conf/bblayers.conf
        sed -i "/immersive-pi/,\$d" conf/local.conf 2>/dev/null || true
        cat /home/builder/build-conf/local-extra.conf >> conf/local.conf
        echo "IMMERSIVE_VERSION = \"$IMMERSIVE_VERSION\"" >> conf/local.conf
        # The Pi kernel is a large, occasionally-flaky git fetch; pre-fetch it
        # with retries before the main build so a transient blip does not waste
        # the whole run. sstate keeps everything else, so this resumes fast.
        for t in 1 2 3 4 5 6; do
            bitbake -c fetch linux-raspberrypi && break
            echo "kernel fetch attempt $t failed; retrying in 20s"; sleep 20
        done
        set -e
        bitbake -c rootfs -f immersive-image
        bitbake immersive-image
        IMG=tmp/deploy/images/immersive-pi/immersive-image-immersive-pi.rootfs
        WIC_SRC=$(readlink -f $IMG.wic)
        ROOTFS_SRC=$(readlink -f $IMG.ext4)
        test -f "$WIC_SRC"    || { echo "BUILD PRODUCED NO WIC IMAGE"; exit 1; }
        test -f "$ROOTFS_SRC" || { echo "BUILD PRODUCED NO ROOTFS"; exit 1; }
        cp "$WIC_SRC" /output/immersive-image-immersive-pi.img

        # Shrink rootfs for OTA bundles.
        SHRUNK=/tmp/rootfs.shrunk.ext4
        cp --sparse=never "$ROOTFS_SRC" "$SHRUNK"
        e2fsck -f -y "$SHRUNK" || true
        resize2fs -M "$SHRUNK"
        e2fsck -f -y "$SHRUNK" || true
        BLOCKS=$(dumpe2fs -h "$SHRUNK" 2>/dev/null | awk -F: "/^Block count/{print \$2}" | tr -d " ")
        BSIZE=$(dumpe2fs -h "$SHRUNK" 2>/dev/null | awk -F: "/^Block size/{print \$2}" | tr -d " ")
        truncate -s $((BLOCKS * BSIZE)) "$SHRUNK"
        cp "$SHRUNK" /output/immersive-rootfs.ext4
        rm -f "$SHRUNK"

        # Extract boot partition contents (first MBR partition) as tar.
        python3 - "$WIC_SRC" /tmp/boot.img <<'\''PY'\''
import struct, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, "rb") as f:
    f.seek(446)
    _,_,_,_,_,_,_,_, start, size = struct.unpack("<BBBBBBBBII", f.read(16))
    f.seek(start * 512)
    with open(dst, "wb") as o:
        remaining = size * 512
        while remaining:
            chunk = f.read(min(remaining, 4 << 20))
            if not chunk: break
            o.write(chunk); remaining -= len(chunk)
PY
        rm -rf /tmp/bootcontents && mkdir -p /tmp/bootcontents
        mcopy -i /tmp/boot.img -s -Q "::/*" /tmp/bootcontents/
        ( cd /tmp/bootcontents && tar cf /output/immersive-boot.tar . )
        [ "$(stat -c%s /output/immersive-boot.tar)" -gt 1048576 ] || { echo "boot.tar too small"; exit 1; }
        rm -rf /tmp/bootcontents /tmp/boot.img
    '

echo ""
echo "Done! Image at: output/immersive-image-immersive-pi.img"
echo "Flash a seed card: sudo dd if=output/immersive-image-immersive-pi.img of=/dev/sdX bs=4M conv=fsync"
echo "Then edit immersive.conf on the boot partition to set each node's role + id."
