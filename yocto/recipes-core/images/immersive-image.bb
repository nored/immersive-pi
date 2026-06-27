SUMMARY = "Immersive 360 node image (render | control, role from boot config)"
DESCRIPTION = "One Raspberry Pi 4 image for the immersive 360 room. Each node \
reads role=render|control from immersive.conf on the boot FAT: render nodes run \
the GStreamer->GLES2 warp/blend->KMS stage, the control node runs the clock \
master, controller, calibration website, and the Node-RED power API."

inherit core-image

IMAGE_FEATURES += "ssh-server-dropbear"

# SSH: install authorized_keys, key-only login.
IMMERSIVE_AUTHORIZED_KEYS ??= "${THISDIR}/../../secrets/authorized_keys"
ROOTFS_POSTPROCESS_COMMAND += "immersive_install_ssh_key;"
immersive_install_ssh_key() {
    if [ -f "${IMMERSIVE_AUTHORIZED_KEYS}" ]; then
        install -d -m 0700 ${IMAGE_ROOTFS}/root/.ssh
        install -m 0600 "${IMMERSIVE_AUTHORIZED_KEYS}" \
            ${IMAGE_ROOTFS}/root/.ssh/authorized_keys
        install -d ${IMAGE_ROOTFS}${sysconfdir}/default
        echo 'DROPBEAR_EXTRA_ARGS="-s -g"' > ${IMAGE_ROOTFS}${sysconfdir}/default/dropbear
    fi
}

IMAGE_INSTALL = " \
    packagegroup-core-boot \
    kernel-modules \
    \
    gstreamer1.0 \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-libav \
    gstreamer1.0-python \
    python3-pygobject \
    \
    mesa-megadriver \
    libgbm \
    libegl-mesa \
    libgles2-mesa \
    libdrm \
    \
    python3 \
    python3-numpy \
    python3-pillow \
    python3-websockets \
    python3-opencv \
    \
    git \
    ffmpeg \
    avahi-daemon \
    avahi-utils \
    chrony \
    dnsmasq \
    \
    immersive \
    immersive-updater \
    immersive-update-key \
    \
    coreutils \
    e2fsprogs \
    ca-certificates \
    tzdata \
    ttf-dejavu-sans \
    ttf-dejavu-sans-mono \
"

# Version baked into /etc/immersive-version — must match the release tag this
# image is published under, so a freshly flashed node does not re-download its
# own version on first poll.
IMMERSIVE_VERSION ?= "pi-v1.0.0"
ROOTFS_POSTPROCESS_COMMAND += "immersive_set_version;"
immersive_set_version() {
    echo "${IMMERSIVE_VERSION}" > ${IMAGE_ROOTFS}${sysconfdir}/immersive-version
}

# Seed a commented immersive.conf onto the boot FAT so the field can set role,
# node id, control host, and the power/API token without reflashing.
ROOTFS_POSTPROCESS_COMMAND += "immersive_seed_bootconf;"
immersive_seed_bootconf() {
    install -d ${IMAGE_ROOTFS}/boot
    if [ -f ${IMAGE_ROOTFS}${datadir}/immersive/immersive.conf-example ]; then
        install -m 0644 ${IMAGE_ROOTFS}${datadir}/immersive/immersive.conf-example \
            ${IMAGE_ROOTFS}/boot/immersive.conf
    fi
}

# dnsmasq only runs on the control node — the role dispatcher starts it. Drop its
# boot auto-start (but leave it startable, so render nodes don't run a DHCP server).
disable_dnsmasq_autostart() {
    rm -f ${IMAGE_ROOTFS}${sysconfdir}/systemd/system/multi-user.target.wants/dnsmasq.service 2>/dev/null || true
}
ROOTFS_POSTPROCESS_COMMAND += "disable_dnsmasq_autostart;"

# Wired network stack: systemd-networkd + systemd-resolved enabled at boot. No
# NetworkManager, no connman (excluded in local.conf). networkd reads the config
# in /etc/systemd/network/ that immersive-net.service writes per role.
enable_systemd_network() {
    install -d ${IMAGE_ROOTFS}${sysconfdir}/systemd/system/multi-user.target.wants \
              ${IMAGE_ROOTFS}${sysconfdir}/systemd/system/sockets.target.wants \
              ${IMAGE_ROOTFS}${sysconfdir}/systemd/system/network-online.target.wants
    ln -sf ${systemd_system_unitdir}/systemd-networkd.service \
        ${IMAGE_ROOTFS}${sysconfdir}/systemd/system/multi-user.target.wants/systemd-networkd.service
    ln -sf ${systemd_system_unitdir}/systemd-networkd.socket \
        ${IMAGE_ROOTFS}${sysconfdir}/systemd/system/sockets.target.wants/systemd-networkd.socket
    ln -sf ${systemd_system_unitdir}/systemd-networkd-wait-online.service \
        ${IMAGE_ROOTFS}${sysconfdir}/systemd/system/network-online.target.wants/systemd-networkd-wait-online.service
    ln -sf ${systemd_system_unitdir}/systemd-resolved.service \
        ${IMAGE_ROOTFS}${sysconfdir}/systemd/system/multi-user.target.wants/systemd-resolved.service
    ln -sf /run/systemd/resolve/stub-resolv.conf ${IMAGE_ROOTFS}${sysconfdir}/resolv.conf
}
ROOTFS_POSTPROCESS_COMMAND += "enable_systemd_network;"

# A/B layout + image types.
IMAGE_FSTYPES = "wic ext4"
WKS_FILE = "immersive-ab.wks.in"

# Headroom for the rootfs slot (gstreamer + mesa + opencv + python). First-boot
# resize grows it to fill the card.
IMAGE_ROOTFS_EXTRA_SPACE = "262144"
