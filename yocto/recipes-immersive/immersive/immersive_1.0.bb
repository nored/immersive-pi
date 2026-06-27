SUMMARY = "Immersive 360 node application (render + control) and role dispatch"
DESCRIPTION = "Installs the render-node and control-node Python apps to \
/opt/immersive and the systemd units that, at boot, read role=render|control \
from immersive.conf and start the matching services."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# The app trees are staged into files/app by build.sh before the Docker build
# (the Docker context is the yocto/ layer, so the app is vendored in here).
SRC_URI = " \
    file://app/ \
    file://immersive-role.sh \
    file://immersive-config.sh \
    file://immersive-role.service \
    file://immersive-render.service \
    file://immersive-control.service \
    file://immersive-clock.service \
    file://eth0-default.network \
    file://immersive.conf-example \
    file://motd.template \
"

S = "${WORKDIR}"

inherit systemd

# Only the role dispatcher is auto-enabled; it starts render OR control services
# based on immersive.conf. Networking is plain DHCP (shipped eth0 config).
SYSTEMD_SERVICE:${PN} = "immersive-role.service"
SYSTEMD_AUTO_ENABLE = "enable"

RDEPENDS:${PN} = " \
    python3 python3-pygobject python3-numpy python3-pillow python3-websockets \
    bash coreutils git \
"

do_install() {
    install -d ${D}/opt/immersive
    cp -r ${WORKDIR}/app/render-node  ${D}/opt/immersive/
    cp -r ${WORKDIR}/app/control-node ${D}/opt/immersive/
    # never ship a dev checkout's concrete config or generated media
    rm -f  ${D}/opt/immersive/render-node/config.json
    find ${D}/opt/immersive -name '__pycache__' -type d -prune -exec rm -rf {} +
    find ${D}/opt/immersive -name '*.mp4' -delete

    install -d ${D}${bindir}
    install -m 0755 ${WORKDIR}/immersive-role.sh    ${D}${bindir}/immersive-role.sh
    install -m 0755 ${WORKDIR}/immersive-config.sh  ${D}${bindir}/immersive-config.sh

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${WORKDIR}/immersive-role.service    ${D}${systemd_system_unitdir}/
    install -m 0644 ${WORKDIR}/immersive-render.service  ${D}${systemd_system_unitdir}/
    install -m 0644 ${WORKDIR}/immersive-control.service ${D}${systemd_system_unitdir}/
    install -m 0644 ${WORKDIR}/immersive-clock.service   ${D}${systemd_system_unitdir}/

    # wired config for every node: DHCP (addresses from the site network)
    install -d ${D}${sysconfdir}/systemd/network
    install -m 0644 ${WORKDIR}/eth0-default.network \
        ${D}${sysconfdir}/systemd/network/10-eth0.network

    # boot-FAT config example + login banner template (version filled by image)
    install -d ${D}${datadir}/immersive
    install -m 0644 ${WORKDIR}/immersive.conf-example ${D}${datadir}/immersive/immersive.conf-example
    install -m 0644 ${WORKDIR}/motd.template          ${D}${datadir}/immersive/motd.template
}

FILES:${PN} = " \
    /opt/immersive \
    ${bindir} \
    ${systemd_system_unitdir} \
    ${sysconfdir}/systemd/network \
    ${datadir}/immersive \
"
