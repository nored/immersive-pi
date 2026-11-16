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
PENDING_MARKER=/var/lib/immersive-resize.pending
[ -f "$DONE_MARKER" ] && { echo "already resized"; exit 0; }

P2_SECTORS=$(cat /sys/class/block/mmcblk0p2/size)
P2_SIZE=$((P2_SECTORS * 512))
if [ "$P2_SIZE" -gt 1800000000 ] && [ ! -f "$PENDING_MARKER" ]; then
    echo "p2 already $P2_SIZE bytes, skipping"
    mkdir -p /var/lib && touch "$DONE_MARKER"; exit 0
fi

# Only the initial A-slot boot may recreate B. Never format a mounted slot.
ROOT_DEV=$(awk '$5 == "/" { print $3; exit }' /proc/self/mountinfo)
[ "$ROOT_DEV" = "$(cat /sys/class/block/mmcblk0p2/dev)" ] || {
    echo "ERROR: resize requires the A slot as root"; exit 1;
}
for SLOT in mmcblk0p3 mmcblk0p4; do
    [ -f "/sys/class/block/$SLOT/dev" ] || continue
    SLOT_DEV=$(cat "/sys/class/block/$SLOT/dev")
    if awk -v device="$SLOT_DEV" '$3 == device { found=1 } END { exit !found }' /proc/self/mountinfo; then
        echo "ERROR: $SLOT is mounted; refusing partition resize"; exit 1
    fi
done

SD_SECTORS=$(cat /sys/class/block/mmcblk0/size)
P2_START=$(cat /sys/class/block/mmcblk0p2/start)
ALIGN=$((4 * 1024 * 1024 / 512))
SAFETY=$((32 * 1024 * 1024 / 512))
USABLE=$((SD_SECTORS - P2_START - SAFETY))
SLOT_SECTORS=$((USABLE / 2))
SLOT_SECTORS=$(( (SLOT_SECTORS / ALIGN) * ALIGN ))
[ "$SLOT_SECTORS" -ge "$P2_SECTORS" ] || { echo "ERROR: refusing to shrink rootfs A"; exit 1; }
P2_END_NEW=$((P2_START + SLOT_SECTORS - 1))
P3_START_NEW=$(( (((P2_END_NEW + 1) + ALIGN - 1) / ALIGN) * ALIGN ))
P3_END_NEW=$((P3_START_NEW + SLOT_SECTORS - 1))
[ "$P3_END_NEW" -lt "$SD_SECTORS" ] || { echo "ERROR: p3_end past disk"; exit 1; }

echo "SD=$SD_SECTORS p2=${P2_START}..${P2_END_NEW} p3=${P3_START_NEW}..${P3_END_NEW}"

# Recreating a DOS table otherwise generates a new identifier and invalidates
# the boot/root PARTUUIDs installed by WIC.
DISK_ID=$(sfdisk --disk-id "$DISK")
printf '%s\n' "$DISK_ID" | grep -Eq '^0x[0-9a-fA-F]{8}$' || {
    echo "ERROR: invalid disk identifier"; exit 1;
}
mkdir -p /var/lib
touch "$PENDING_MARKER"
sync
sfdisk --no-reread "$DISK" <<EOF
label: dos
label-id: ${DISK_ID}
unit: sectors

start=$(cat /sys/class/block/mmcblk0p1/start), size=$(cat /sys/class/block/mmcblk0p1/size), type=c, bootable
start=${P2_START}, size=$((P2_END_NEW - P2_START + 1)), type=83
start=${P3_START_NEW}, size=$((P3_END_NEW - P3_START_NEW + 1)), type=83
EOF

partprobe "$DISK" || true
sleep 1
# A failed kernel table refresh must never send mkfs to the old B offset.
if [ "$(cat /sys/class/block/mmcblk0p2/start)" != "$P2_START" ] ||
   [ "$(cat /sys/class/block/mmcblk0p2/size)" != "$SLOT_SECTORS" ] ||
   [ "$(cat /sys/class/block/mmcblk0p3/start)" != "$P3_START_NEW" ] ||
   [ "$(cat /sys/class/block/mmcblk0p3/size)" != "$SLOT_SECTORS" ]; then
    echo "Kernel partition view is stale; rebooting before filesystem changes"
    systemctl reboot --no-block
    exit 0
fi
resize2fs "$P2"
mkfs.ext4 -F -L rootfsB "$P3"
mkdir -p /var/lib && touch "$DONE_MARKER"
rm -f "$PENDING_MARKER"
sync
echo "resize complete"
