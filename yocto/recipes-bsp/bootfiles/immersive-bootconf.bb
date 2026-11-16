SUMMARY = "Provisioning template on the actual Raspberry Pi boot FAT"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

FILESEXTRAPATHS:prepend := "${THISDIR}/../../recipes-immersive/immersive/files:"
SRC_URI = "file://immersive.conf-example"
S = "${WORKDIR}"

inherit deploy

do_install[noexec] = "1"
do_deploy() {
    install -d ${DEPLOYDIR}
    install -m 0644 ${WORKDIR}/immersive.conf-example ${DEPLOYDIR}/immersive.conf
}
addtask deploy after do_install before do_build
ALLOW_EMPTY:${PN} = "1"
