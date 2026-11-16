FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-gi python3-numpy python3-pil python3-opengl \
    python3-websockets python3-pytest \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-libav \
    libegl1 libgles2 libgl1-mesa-dri ffmpeg \
    && rm -rf /var/lib/apt/lists/*
ENV PYOPENGL_PLATFORM=egl EGL_PLATFORM=surfaceless
WORKDIR /workspace
CMD ["python3", "-m", "pytest", "-q", "tests"]
