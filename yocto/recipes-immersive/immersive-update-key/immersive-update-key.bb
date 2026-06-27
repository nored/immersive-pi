SUMMARY = "Per-image symmetric key for immersive update artifacts"
DESCRIPTION = "Installs the 32-byte key used to verify and decrypt OTA bundles \
published to the immersive GitHub release repo. Read from secrets/update.key in \
the layer at build time; never fetched."
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# Secret path on the build host — not in SRC_URI, never fetched.
IMMERSIVE_UPDATE_KEY_PATH ??= "${THISDIR}/../../secrets/update.key"

S = "${WORKDIR}"

do_install() {
    if [ ! -f "${IMMERSIVE_UPDATE_KEY_PATH}" ]; then
        bbfatal "Update key not found at ${IMMERSIVE_UPDATE_KEY_PATH} — generate one with 'head -c 32 /dev/urandom > secrets/update.key' before building."
    fi
    install -d ${D}${sysconfdir}
    install -m 0600 "${IMMERSIVE_UPDATE_KEY_PATH}" ${D}${sysconfdir}/immersive-update.key
}

FILES:${PN} = "${sysconfdir}/immersive-update.key"

# Machine-specific so a key change retriggers the rebuild.
PACKAGE_ARCH = "${MACHINE_ARCH}"
RDEPENDS:${PN} = ""
