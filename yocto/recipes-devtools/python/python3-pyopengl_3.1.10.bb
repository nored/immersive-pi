SUMMARY = "PyOpenGL bindings including the EGL and GLES2 APIs used by the renderer"
HOMEPAGE = "https://pyopengl.sourceforge.net/"
LICENSE = "BSD-3-Clause"
LIC_FILES_CHKSUM = "file://license.txt;md5=943332dbb441a49d1576fe75197d6cac"

PYPI_PACKAGE = "pyopengl"
SRC_URI[sha256sum] = "c4a02d6866b54eb119c8e9b3fb04fa835a95ab802dd96607ab4cdb0012df8335"

inherit pypi setuptools3

RDEPENDS:${PN} = "python3-core python3-ctypes python3-logging python3-numpy python3-pkgutil"

do_install:append() {
    # These optional Windows DLLs are never used by the EGL backend on Linux.
    rm -rf ${D}${PYTHON_SITEPACKAGES_DIR}/OpenGL/DLLS
    test -f ${D}${PYTHON_SITEPACKAGES_DIR}/OpenGL/EGL/__init__.py
    test -f ${D}${PYTHON_SITEPACKAGES_DIR}/OpenGL/GLES2/__init__.py
    test -f ${D}${PYTHON_SITEPACKAGES_DIR}/OpenGL/raw/GLES2/__init__.py
}
