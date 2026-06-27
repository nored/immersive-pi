#!/bin/bash
cd /home/builder

sudo chown builder:builder /home/builder/build 2>/dev/null || true
sudo chown builder:builder /output 2>/dev/null || true

source poky/oe-init-build-env build > /dev/null 2>&1

# Apply our config (volume may be empty on first run).
cp /home/builder/build-conf/bblayers.conf conf/bblayers.conf
sed -i "/immersive-pi/,\$d" conf/local.conf 2>/dev/null || true
cat /home/builder/build-conf/local-extra.conf >> conf/local.conf
echo "IMMERSIVE_VERSION = \"${IMMERSIVE_VERSION:-pi-v1.0.0}\"" >> conf/local.conf

bitbake immersive-image || exit 1

WIC_SRC=$(readlink -f tmp/deploy/images/immersive-pi/immersive-image-immersive-pi.rootfs.wic)
test -f "$WIC_SRC" || { echo "BUILD PRODUCED NO WIC IMAGE"; exit 1; }
cp "$WIC_SRC" /output/immersive-image-immersive-pi.img
echo "Build complete! Image: /output/immersive-image-immersive-pi.img"
ls -lh /output/immersive-image-immersive-pi.img
