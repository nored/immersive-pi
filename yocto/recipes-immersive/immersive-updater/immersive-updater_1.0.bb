SUMMARY = "Immersive 360 A/B updater (downloads encrypted releases from GitHub)"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

SRC_URI = " \
    file://immersive-updater \
    file://immersive-updater-confirm \
    file://immersive-resize.sh \
"

S = "${WORKDIR}"

inherit systemd

RDEPENDS:${PN} = " \
    curl openssl-bin zstd coreutils sed gawk \
    e2fsprogs e2fsprogs-resize2fs e2fsprogs-mke2fs \
    util-linux util-linux-sfdisk parted \
"

do_install() {
    install -d ${D}${bindir}
    install -m 0755 ${WORKDIR}/immersive-updater         ${D}${bindir}/immersive-updater
    install -m 0755 ${WORKDIR}/immersive-updater-confirm ${D}${bindir}/immersive-updater-confirm
    install -m 0755 ${WORKDIR}/immersive-resize.sh       ${D}${bindir}/immersive-resize.sh

    install -d ${D}${systemd_system_unitdir}

    cat > ${D}${systemd_system_unitdir}/immersive-updater.service << 'EOF'
[Unit]
Description=Immersive A/B updater (download + flash inactive slot)
After=network-online.target
Wants=network-online.target
ConditionPathExists=/etc/immersive-update.key

[Service]
Type=oneshot
ExecStart=/usr/bin/immersive-updater
StandardOutput=journal
StandardError=journal
EOF

    cat > ${D}${systemd_system_unitdir}/immersive-updater.timer << 'EOF'
[Unit]
Description=Run the immersive updater nightly

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=30m
# No Persistent: a missed window (room powered off) waits for the next one
# rather than firing right after a wake, so a hibernating room is never updated
# + rebooted out from under a show.

[Install]
WantedBy=timers.target
EOF

    cat > ${D}${systemd_system_unitdir}/immersive-updater-confirm.service << 'EOF'
[Unit]
Description=Confirm or roll back an A/B update
After=immersive-role.service
Wants=immersive-role.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/immersive-updater-confirm

[Install]
WantedBy=multi-user.target
EOF

    cat > ${D}${systemd_system_unitdir}/immersive-resize.service << 'EOF'
[Unit]
Description=First-boot partition resize to fill the SD card
DefaultDependencies=no
After=local-fs.target
Before=immersive-role.service immersive-updater.timer
ConditionPathExists=!/var/lib/immersive-resize.done

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/immersive-resize.sh

[Install]
WantedBy=multi-user.target
EOF
}

FILES:${PN} = " \
    ${bindir}/immersive-updater \
    ${bindir}/immersive-updater-confirm \
    ${bindir}/immersive-resize.sh \
    ${systemd_system_unitdir}/immersive-updater.service \
    ${systemd_system_unitdir}/immersive-updater.timer \
    ${systemd_system_unitdir}/immersive-updater-confirm.service \
    ${systemd_system_unitdir}/immersive-resize.service \
"

SYSTEMD_SERVICE:${PN} = "immersive-updater.timer immersive-updater-confirm.service immersive-resize.service"
SYSTEMD_AUTO_ENABLE = "enable"
