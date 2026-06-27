#!/bin/sh
# Read immersive.conf from the boot FAT (survives an OTA that rewrites rootfs)
# and materialise the per-node runtime config under /run. Same lookup order the
# render/control apps expect.
set -eu

CONF=""
for c in /boot/firmware/immersive.conf /boot/immersive.conf /etc/immersive.conf; do
    [ -r "$c" ] && { CONF="$c"; break; }
done

get() { [ -n "$CONF" ] && sed -n "s/^$1=//p" "$CONF" | tr -d '\r' | head -n1 || true; }

ROLE=$(get role);          ROLE=${ROLE:-render}
NODE=$(get node);          NODE=${NODE:-$(hostname)}
CTRL=$(get control_host);  CTRL=${CTRL:-pi-13.local}
HOST=$(get hostname);      HOST=${HOST:-$NODE}
TOKEN=$(get api_token)
ALLOW=$(get allow_poweroff); ALLOW=${ALLOW:-true}
TZ=$(get timezone)

mkdir -p /run/immersive
echo "$ROLE" > /run/immersive/role

# hostname (mDNS <hostname>.local via avahi)
hostnamectl set-hostname "$HOST" 2>/dev/null || echo "$HOST" > /etc/hostname

# optional timezone
[ -n "${TZ:-}" ] && ln -sf "/usr/share/zoneinfo/$TZ" /etc/localtime 2>/dev/null || true

# render node config.json — written to /run so it works even if / is read-only
cat > /run/immersive/config.json <<EOF
{
  "node": "$NODE",
  "control_host": "$CTRL",
  "control_ws_port": 8765,
  "clock_port": 8555,
  "media_dir": "/opt/immersive/media",
  "drm": { "device": "/dev/dri/card0", "connector": "auto" },
  "preview": { "width": 320, "height": 180, "fps": 8, "jpeg_quality": 60 },
  "heartbeat_hz": 1,
  "loop": true,
  "allow_poweroff": $ALLOW
}
EOF

# control node environment (token for the Node-RED power API)
echo "IMMERSIVE_API_TOKEN=${TOKEN:-}" > /run/immersive/control.env

# (Networking is plain DHCP via the shipped /etc/systemd/network/10-eth0.network.)
echo "immersive-config: role=$ROLE node=$NODE control=$CTRL host=$HOST"
