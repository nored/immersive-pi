#!/bin/sh
# Early network setup — runs BEFORE systemd-networkd so the link is configured
# correctly on the first boot. Wired only; no NetworkManager, no connman.
#   control = static IP (it is the DHCP server + gateway for the switch)
#   render  = DHCP client (its reserved address comes from the control node)
set -eu

CONF=""
for c in /boot/firmware/immersive.conf /boot/immersive.conf /etc/immersive.conf; do
    [ -r "$c" ] && { CONF="$c"; break; }
done
get() { [ -n "$CONF" ] && sed -n "s/^$1=//p" "$CONF" | tr -d '\r' | head -n1 || true; }

ROLE=$(get role);          ROLE=${ROLE:-render}
CTRL_IP=$(get control_ip); CTRL_IP=${CTRL_IP:-10.0.0.13}

mkdir -p /etc/systemd/network
if [ "$ROLE" = "control" ]; then
    cat > /etc/systemd/network/10-eth0.network <<EOF
[Match]
Name=eth0

[Network]
Address=$CTRL_IP/24
DHCPServer=no
EOF
    # let dnsmasq read the reservations the controller writes
    grep -q '^conf-dir=/etc/dnsmasq.d' /etc/dnsmasq.conf 2>/dev/null \
        || echo 'conf-dir=/etc/dnsmasq.d' >> /etc/dnsmasq.conf
else
    cat > /etc/systemd/network/10-eth0.network <<EOF
[Match]
Name=eth0

[Network]
DHCP=ipv4
EOF
fi
echo "immersive-net: role=$ROLE eth0 configured"
