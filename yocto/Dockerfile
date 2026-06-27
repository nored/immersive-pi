FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    gawk wget git diffstat unzip texinfo gcc build-essential \
    chrpath socat cpio python3 python3-pip python3-pexpect \
    xz-utils debianutils iputils-ping python3-git python3-jinja2 \
    python3-subunit zstd liblz4-tool file locales libacl1 sudo \
    lz4 mtools \
    && rm -rf /var/lib/apt/lists/*

RUN locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8

# Yocto refuses to run as root.
RUN useradd -m builder && echo "builder ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers
USER builder
WORKDIR /home/builder

ARG YOCTO_RELEASE=scarthgap

# HTTPS mirrors (git:// endpoints are often firewalled).
RUN git clone --depth 1 -b ${YOCTO_RELEASE} https://github.com/yoctoproject/poky.git && \
    git clone --depth 1 -b ${YOCTO_RELEASE} https://github.com/agherzan/meta-raspberrypi.git && \
    git clone --depth 1 -b ${YOCTO_RELEASE} https://github.com/openembedded/meta-openembedded.git

# The build context is the yocto/ layer (build.sh vendors the app into
# recipes-immersive/immersive/files/app first).
COPY --chown=builder:builder . /home/builder/meta-immersive

RUN mkdir -p /home/builder/build-conf && \
    cat > /home/builder/build-conf/bblayers.conf << 'LAYERS'
POKY_BBLAYERS_CONF_VERSION = "2"
BBPATH = "${TOPDIR}"
BBFILES ?= ""
BBLAYERS = " \
  /home/builder/poky/meta \
  /home/builder/poky/meta-poky \
  /home/builder/poky/meta-yocto-bsp \
  /home/builder/meta-openembedded/meta-oe \
  /home/builder/meta-openembedded/meta-python \
  /home/builder/meta-openembedded/meta-networking \
  /home/builder/meta-openembedded/meta-multimedia \
  /home/builder/meta-raspberrypi \
  /home/builder/meta-immersive \
"
LAYERS

RUN cat > /home/builder/build-conf/local-extra.conf << 'LOCAL'
MACHINE = "immersive-pi"
# Headless GLES2 over GBM/DRM: opengl, no x11/wayland compositor, systemd init.
DISTRO_FEATURES:remove = "x11 vulkan wayland"
DISTRO_FEATURES:append = " opengl systemd pam"
INIT_MANAGER = "systemd"
PREFERRED_VERSION_linux-raspberrypi = "6.12%"
# opencv with the python bindings (control-node structured-light scan).
PACKAGECONFIG:append:pn-opencv = " python3"
# opencv's C++ TUs are memory-heavy; building 8 in parallel OOM-kills cc1plus.
# Throttle just opencv (same pattern yBrowser uses for webkitgtk).
PARALLEL_MAKE:pn-opencv = "-j 2"
IMAGE_FSTYPES = "wic ext4"
WKS_FILE = "immersive-ab.wks.in"
LICENSE_FLAGS_ACCEPTED = "commercial"
BB_NUMBER_THREADS ?= "8"
PARALLEL_MAKE ?= "-j 8"
INHERIT += "rm_work"
LOCAL

COPY --chown=builder:builder yocto-entrypoint.sh /home/builder/entrypoint.sh
RUN chmod +x /home/builder/entrypoint.sh

CMD ["/home/builder/entrypoint.sh"]
