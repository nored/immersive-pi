#!/bin/sh
# First-boot partition resize. The wic image ships small partitions so the
# flashed image is small; on first boot grow rootfs A in place and recreate an
# empty rootfs B sized to match, leaving room to stage OTA bundles.
#   p1 boot  256 MB (unchanged)
#   p2 rootA (SD - p2_start)/2  online grow
#   p3 rootB (SD - p2_start)/2  recreated empty (for A/B OTA)
set -eu

LOG=/tmp/immersive-resize.log
exec >>"$LOG" 2>&1
echo "=== immersive-resize $(date 2>/dev/null) ==="
set -x

DISK=/dev/mmcblk0
P2=/dev/mmcblk0p2
P3=/dev/mmcblk0p3
DONE_MARKER=/var/lib/immersive-resize.done
[ -f "$DONE_MARKER" ] && { echo "already resized"; exit 0; }

P2_SECTORS=$(cat /sys/class/block/mmcblk0p2/size)
P2_SIZE=$((P2_SECTORS * 512))
if [ "$P2_SIZE" -gt 1800000000 ]; then
    echo "p2 already $P2_SIZE bytes, skipping"
    mkdir -p /var/lib && touch "$DONE_MARKER"; exit 0
fi

SD_SECTORS=$(cat /sys/class/block/mmcblk0/size)
P2_START=$(cat /sys/class/block/mmcblk0p2/start)
ALIGN=$((4 * 1024 * 1024 / 512))
SAFETY=$((32 * 1024 * 1024 / 512))
USABLE=$((SD_SECTORS - P2_START - SAFETY))
SLOT_SECTORS=$((USABLE / 2))
SLOT_SECTORS=$(( (SLOT_SECTORS / ALIGN) * ALIGN ))
P2_END_NEW=$((P2_START + SLOT_SECTORS - 1))
P3_START_NEW=$(( (((P2_END_NEW + 1) + ALIGN - 1) / ALIGN) * ALIGN ))
P3_END_NEW=$((P3_START_NEW + SLOT_SECTORS - 1))
[ "$P3_END_NEW" -lt "$SD_SECTORS" ] || { echo "ERROR: p3_end past disk"; exit 1; }

echo "SD=$SD_SECTORS p2=${P2_START}..${P2_END_NEW} p3=${P3_START_NEW}..${P3_END_NEW}"

sfdisk --no-reread "$DISK" <<EOF
label: dos
unit: sectors

start=$(cat /sys/class/block/mmcblk0p1/start), size=$(cat /sys/class/block/mmcblk0p1/size), type=c, bootable
start=${P2_START}, size=$((P2_END_NEW - P2_START + 1)), type=83
start=${P3_START_NEW}, size=$((P3_END_NEW - P3_START_NEW + 1)), type=83
EOF

partprobe "$DISK" || true
sleep 1
resize2fs "$P2"
mkfs.ext4 -F -L rootfsB "$P3"
mkdir -p /var/lib && touch "$DONE_MARKER"
sync
echo "resize complete"
