"""drm_kms.py — native DRM/GBM/EGL bring-up for headless GLES2 scanout.

A thin ctypes wrapper around libdrm, libgbm, and libEGL that does what kmscube
does: pick a connected connector and its preferred mode, create a GBM scanout
surface, bind an EGL/GLES2 context to it, and page-flip each rendered buffer to
the CRTC. No compositor, no kmssink — this stage owns the display.

This is the one component that must be confirmed on the chosen board during
Milestone 1 (Pi 4 vs Pi 5; vc4/v3d KMS driver). The Python logic above it
(warp, blend, preview, control plane) is board-independent.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import os
import struct
import time

from shared.display_modes import select_mode

# ---- library handles -------------------------------------------------------
_libdrm = ctypes.CDLL(ctypes.util.find_library("drm") or "libdrm.so.2", use_errno=True)
_libgbm = ctypes.CDLL(ctypes.util.find_library("gbm") or "libgbm.so.1", use_errno=True)
_libegl = ctypes.CDLL(ctypes.util.find_library("EGL") or "libEGL.so.1", use_errno=True)

# ---- DRM constants ---------------------------------------------------------
DRM_MODE_CONNECTED = 1
DRM_FORMAT_XRGB8888 = struct.unpack("<I", b"XR24")[0]
GBM_BO_USE_SCANOUT = 1 << 0
GBM_BO_USE_RENDERING = 1 << 2
DRM_MODE_PAGE_FLIP_EVENT = 0x01
# Linux UAPI drm.h: _IOWR('d', 0x3b, struct drm_crtc_get_sequence).
_SEQUENCE_FORMAT = '=IIQq'
DRM_IOCTL_CRTC_GET_SEQUENCE = (3 << 30) | (struct.calcsize(_SEQUENCE_FORMAT) << 16) | (ord('d') << 8) | 0x3b


def current_refresh_ns(fd, crtc_id):
    """Read the active CRTC's scanout phase without scheduling an event."""
    request = bytearray(struct.pack(_SEQUENCE_FORMAT, crtc_id, 0, 0, 0))
    try:
        fcntl.ioctl(fd, DRM_IOCTL_CRTC_GET_SEQUENCE, request, True)
    except OSError as exc:
        if exc.errno in (errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOENT):
            return None
        raise
    identity, active, sequence, timestamp = struct.unpack(_SEQUENCE_FORMAT, request)
    return timestamp if identity == crtc_id and active and timestamp > 0 else None

# ---- EGL constants ---------------------------------------------------------
EGL_PLATFORM_GBM_KHR = 0x31D7
EGL_SURFACE_TYPE = 0x3033
EGL_WINDOW_BIT = 0x0004
EGL_RENDERABLE_TYPE = 0x3040
EGL_OPENGL_ES2_BIT = 0x0004
EGL_RED_SIZE = 0x3024
EGL_GREEN_SIZE = 0x3023
EGL_BLUE_SIZE = 0x3022
EGL_ALPHA_SIZE = 0x3021
EGL_NONE = 0x3038
EGL_CONTEXT_CLIENT_VERSION = 0x3098
EGL_NO_CONTEXT = ctypes.c_void_p(0)
EGL_NATIVE_VISUAL_ID = 0x302E


# ---- DRM structs (only the fields we read) ---------------------------------
class drmModeModeInfo(ctypes.Structure):
    _fields_ = [
        ("clock", ctypes.c_uint32),
        ("hdisplay", ctypes.c_uint16), ("hsync_start", ctypes.c_uint16),
        ("hsync_end", ctypes.c_uint16), ("htotal", ctypes.c_uint16),
        ("hskew", ctypes.c_uint16),
        ("vdisplay", ctypes.c_uint16), ("vsync_start", ctypes.c_uint16),
        ("vsync_end", ctypes.c_uint16), ("vtotal", ctypes.c_uint16),
        ("vscan", ctypes.c_uint16),
        ("vrefresh", ctypes.c_uint32),
        ("flags", ctypes.c_uint32), ("type", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
    ]


class drmModeRes(ctypes.Structure):
    _fields_ = [
        ("count_fbs", ctypes.c_int), ("fbs", ctypes.POINTER(ctypes.c_uint32)),
        ("count_crtcs", ctypes.c_int), ("crtcs", ctypes.POINTER(ctypes.c_uint32)),
        ("count_connectors", ctypes.c_int), ("connectors", ctypes.POINTER(ctypes.c_uint32)),
        ("count_encoders", ctypes.c_int), ("encoders", ctypes.POINTER(ctypes.c_uint32)),
        ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
        ("min_height", ctypes.c_uint32), ("max_height", ctypes.c_uint32),
    ]


class drmModeConnector(ctypes.Structure):
    _fields_ = [
        ("connector_id", ctypes.c_uint32), ("encoder_id", ctypes.c_uint32),
        ("connector_type", ctypes.c_uint32), ("connector_type_id", ctypes.c_uint32),
        ("connection", ctypes.c_uint32),
        ("mmWidth", ctypes.c_uint32), ("mmHeight", ctypes.c_uint32),
        ("subpixel", ctypes.c_uint32),
        ("count_modes", ctypes.c_int), ("modes", ctypes.POINTER(drmModeModeInfo)),
        ("count_props", ctypes.c_int), ("props", ctypes.POINTER(ctypes.c_uint32)),
        ("prop_values", ctypes.POINTER(ctypes.c_uint64)),
        ("count_encoders", ctypes.c_int), ("encoders", ctypes.POINTER(ctypes.c_uint32)),
    ]


class drmModeEncoder(ctypes.Structure):
    _fields_ = [
        ("encoder_id", ctypes.c_uint32), ("encoder_type", ctypes.c_uint32),
        ("crtc_id", ctypes.c_uint32), ("possible_crtcs", ctypes.c_uint32),
        ("possible_clones", ctypes.c_uint32),
    ]


# ---- prototypes ------------------------------------------------------------
_libdrm.drmModeGetResources.restype = ctypes.POINTER(drmModeRes)
_libdrm.drmModeGetResources.argtypes = [ctypes.c_int]
_libdrm.drmModeGetConnector.restype = ctypes.POINTER(drmModeConnector)
_libdrm.drmModeGetConnector.argtypes = [ctypes.c_int, ctypes.c_uint32]
_libdrm.drmModeGetEncoder.restype = ctypes.POINTER(drmModeEncoder)
_libdrm.drmModeGetEncoder.argtypes = [ctypes.c_int, ctypes.c_uint32]
_libdrm.drmModeAddFB.restype = ctypes.c_int
_libdrm.drmModeAddFB.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint32,
                                 ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
_libdrm.drmModeSetCrtc.restype = ctypes.c_int
_libdrm.drmModeSetCrtc.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
                                   ctypes.POINTER(drmModeModeInfo)]
_libdrm.drmModePageFlip.restype = ctypes.c_int
_libdrm.drmModePageFlip.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                                    ctypes.c_uint32, ctypes.c_void_p]
_libdrm.drmModeRmFB.argtypes = [ctypes.c_int, ctypes.c_uint32]

_libgbm.gbm_create_device.restype = ctypes.c_void_p
_libgbm.gbm_create_device.argtypes = [ctypes.c_int]
_libgbm.gbm_surface_create.restype = ctypes.c_void_p
_libgbm.gbm_surface_create.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
_libgbm.gbm_surface_lock_front_buffer.restype = ctypes.c_void_p
_libgbm.gbm_surface_lock_front_buffer.argtypes = [ctypes.c_void_p]
_libgbm.gbm_surface_release_buffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_libgbm.gbm_bo_get_width.restype = ctypes.c_uint32
_libgbm.gbm_bo_get_width.argtypes = [ctypes.c_void_p]
_libgbm.gbm_bo_get_height.restype = ctypes.c_uint32
_libgbm.gbm_bo_get_height.argtypes = [ctypes.c_void_p]
_libgbm.gbm_bo_get_stride.restype = ctypes.c_uint32
_libgbm.gbm_bo_get_stride.argtypes = [ctypes.c_void_p]
_libgbm.gbm_bo_get_handle.restype = ctypes.c_uint64  # union; low 32 bits = handle
_libgbm.gbm_bo_get_handle.argtypes = [ctypes.c_void_p]

_libegl.eglGetProcAddress.restype = ctypes.c_void_p
_libegl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
_libegl.eglInitialize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                  ctypes.POINTER(ctypes.c_int)]
_libegl.eglChooseConfig.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                    ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_int)]
_libegl.eglCreateContext.restype = ctypes.c_void_p
_libegl.eglCreateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.c_int)]
_libegl.eglCreateWindowSurface.restype = ctypes.c_void_p
_libegl.eglCreateWindowSurface.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
_libegl.eglMakeCurrent.argtypes = [ctypes.c_void_p] * 4
_libegl.eglSwapBuffers.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_libegl.eglBindAPI.argtypes = [ctypes.c_uint]
EGL_OPENGL_ES_API = 0x30A0


def _open_card(path):
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    return fd


class Backend:
    """Owns the DRM fd, GBM surface, and EGL context for one projector output."""

    def __init__(self, card="/dev/dri/card0", connector_pref="auto", mode=None):
        self.card = card
        self.connector_pref = connector_pref
        self.requested_mode = mode or os.environ.get("IMMERSIVE_DRM_MODE")
        self.fd = None
        self.crtc_id = None
        self.connector_id = None
        self.mode = None
        self.width = self.height = 0
        self.gbm_dev = None
        self.gbm_surface = None
        self.egl_dpy = None
        self.egl_ctx = None
        self.egl_surf = None
        self.egl_config = ctypes.c_void_p()
        self._prev_bo = None
        self._fb_for_bo = {}
        self.last_flip_ns = None
        self._pending_bo = None
        self._flip_submitted = 0

    # ---- modeset ---------------------------------------------------------
    def modeset(self):
        self.fd = _open_card(self.card)
        res = _libdrm.drmModeGetResources(self.fd)
        if not res:
            raise RuntimeError(f"drmModeGetResources failed on {self.card}")
        res = res.contents
        conn = None
        for i in range(res.count_connectors):
            c = _libdrm.drmModeGetConnector(self.fd, res.connectors[i]).contents
            name = f"HDMI-A-{c.connector_type_id}" if c.connector_type == 11 else str(c.connector_id)
            if self.connector_pref not in ("auto", name, str(c.connector_id)):
                continue
            if c.connection == DRM_MODE_CONNECTED and c.count_modes > 0:
                try:
                    selected = select_mode([c.modes[j] for j in range(c.count_modes)], self.requested_mode)
                except ValueError:
                    continue
                conn = c
                break
        if conn is None:
            raise RuntimeError(f"no connected connector supports {self.requested_mode or 'a display mode'}; check HDMI/EDID")
        self.connector_id = conn.connector_id
        self.mode = selected
        self.width = self.mode.hdisplay
        self.height = self.mode.vdisplay
        enc = _libdrm.drmModeGetEncoder(self.fd, conn.encoder_id)
        self.crtc_id = enc.contents.crtc_id if enc else None
        if not self.crtc_id:
            # fall back to first CRTC
            self.crtc_id = res.crtcs[0]

        self.gbm_dev = _libgbm.gbm_create_device(self.fd)
        if not self.gbm_dev:
            raise RuntimeError("gbm_create_device failed")
        self.gbm_surface = _libgbm.gbm_surface_create(
            self.gbm_dev, self.width, self.height, DRM_FORMAT_XRGB8888,
            GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING)
        if not self.gbm_surface:
            raise RuntimeError("gbm_surface_create failed")
        return self.width, self.height

    # ---- EGL -------------------------------------------------------------
    def egl_init(self):
        get_platform_display = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
        )(_libegl.eglGetProcAddress(b"eglGetPlatformDisplayEXT"))
        self.egl_dpy = get_platform_display(EGL_PLATFORM_GBM_KHR, self.gbm_dev, None)
        if not self.egl_dpy:
            raise RuntimeError("eglGetPlatformDisplayEXT failed")
        major = ctypes.c_int()
        minor = ctypes.c_int()
        if not _libegl.eglInitialize(self.egl_dpy, ctypes.byref(major), ctypes.byref(minor)):
            raise RuntimeError("eglInitialize failed")
        _libegl.eglBindAPI(EGL_OPENGL_ES_API)

        attribs = (ctypes.c_int * 13)(
            EGL_SURFACE_TYPE, EGL_WINDOW_BIT,
            EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 0,
            EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
            EGL_NONE)
        num = ctypes.c_int()
        # pick a config whose native visual matches XRGB8888 so the GBM buffer
        # is directly scannable
        n_configs = 32
        configs = (ctypes.c_void_p * n_configs)()
        _libegl.eglChooseConfig(self.egl_dpy, attribs, configs, n_configs, ctypes.byref(num))
        self.egl_config = self._match_visual(configs, num.value)

        ctx_attribs = (ctypes.c_int * 3)(EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE)
        self.egl_ctx = _libegl.eglCreateContext(self.egl_dpy, self.egl_config,
                                                EGL_NO_CONTEXT, ctx_attribs)
        if not self.egl_ctx:
            raise RuntimeError("eglCreateContext failed")
        self.egl_surf = _libegl.eglCreateWindowSurface(
            self.egl_dpy, self.egl_config, ctypes.c_void_p(self.gbm_surface), None)
        if not self.egl_surf:
            raise RuntimeError("eglCreateWindowSurface failed")
        self.make_current()

    def _match_visual(self, configs, count):
        get_attrib = _libegl.eglGetConfigAttrib
        get_attrib.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                               ctypes.POINTER(ctypes.c_int)]
        val = ctypes.c_int()
        for i in range(count):
            get_attrib(self.egl_dpy, configs[i], EGL_NATIVE_VISUAL_ID, ctypes.byref(val))
            if val.value == DRM_FORMAT_XRGB8888:
                return ctypes.c_void_p(configs[i])
        return ctypes.c_void_p(configs[0]) if count else None

    def make_current(self):
        if not _libegl.eglMakeCurrent(self.egl_dpy, self.egl_surf, self.egl_surf, self.egl_ctx):
            raise RuntimeError("eglMakeCurrent failed")

    # ---- present ---------------------------------------------------------
    def _fb_for(self, bo):
        if bo in self._fb_for_bo:
            return self._fb_for_bo[bo]
        handle = _libgbm.gbm_bo_get_handle(bo) & 0xFFFFFFFF
        stride = _libgbm.gbm_bo_get_stride(bo)
        fb = ctypes.c_uint32()
        handles = (ctypes.c_uint32 * 4)(handle, 0, 0, 0)
        # legacy AddFB: depth 24, bpp 32
        r = _libdrm.drmModeAddFB(self.fd, self.width, self.height, 24, 32,
                                 stride, handle, ctypes.byref(fb))
        if r != 0:
            raise RuntimeError("drmModeAddFB failed")
        self._fb_for_bo[bo] = fb.value
        return fb.value

    def queue_flip(self):
        """Submit without waiting so GPU staging can overlap scanout/vblank."""
        if self._pending_bo is not None:
            raise RuntimeError("a DRM page flip is already pending")
        return self.queue_buffer(self.stage_buffer())

    def stage_buffer(self):
        """Lock a fully rendered native scanout buffer without displaying it."""
        if not _libegl.eglSwapBuffers(self.egl_dpy, self.egl_surf):
            raise RuntimeError("eglSwapBuffers failed")
        bo = _libgbm.gbm_surface_lock_front_buffer(self.gbm_surface)
        if not bo:
            raise RuntimeError("gbm_surface_lock_front_buffer failed")
        try:
            return bo, self._fb_for(bo)
        except Exception:
            _libgbm.gbm_surface_release_buffer(self.gbm_surface, bo)
            raise

    def release_buffer(self, token):
        _libgbm.gbm_surface_release_buffer(self.gbm_surface, token[0])

    def queue_buffer(self, token):
        bo, fb = token
        if self._pending_bo is not None:
            self.release_buffer(token)
            raise RuntimeError("a DRM page flip is already pending")
        if self._prev_bo is None:
            conn = (ctypes.c_uint32 * 1)(self.connector_id)
            result = _libdrm.drmModeSetCrtc(self.fd, self.crtc_id, fb, 0, 0, conn, 1,
                                           ctypes.pointer(self.mode))
            if result:
                _libgbm.gbm_surface_release_buffer(self.gbm_surface, bo)
                raise OSError(ctypes.get_errno(), "drmModeSetCrtc failed")
            self._prev_bo = bo
            self.last_flip_ns = time.monotonic_ns()
            return self.last_flip_ns
        if _libdrm.drmModePageFlip(self.fd, self.crtc_id, fb, DRM_MODE_PAGE_FLIP_EVENT, None):
            _libgbm.gbm_surface_release_buffer(self.gbm_surface, bo)
            raise OSError(ctypes.get_errno(), "drmModePageFlip failed")
        self._pending_bo = bo
        self._flip_submitted = time.monotonic()
        return None

    def _finish_flip(self, timestamp):
        _libgbm.gbm_surface_release_buffer(self.gbm_surface, self._prev_bo)
        self._prev_bo, self._pending_bo = self._pending_bo, None
        self.last_flip_ns = timestamp
        return timestamp

    def poll_flip(self):
        if self._pending_bo is None:
            return None
        timestamp = _read_flip(self.fd, 0)
        if timestamp is not None:
            return self._finish_flip(timestamp)
        if time.monotonic() - self._flip_submitted > .25:
            raise TimeoutError("DRM page flip did not complete within 250ms")
        return None

    def current_refresh_ns(self):
        return current_refresh_ns(self.fd, self.crtc_id)

    def swap_and_flip(self):
        if self._pending_bo is not None:
            self._finish_flip(_drain_flip(self.fd))
        timestamp = self.queue_flip()
        return timestamp if timestamp is not None else self._finish_flip(_drain_flip(self.fd))

    def blank(self):
        """Disable the CRTC so the connector goes to no-signal — the projector
        drops to standby. The next present re-runs the full modeset (we clear
        _prev_bo) to bring it back."""
        if self.fd is not None and self.crtc_id:
            if self._pending_bo is not None:
                self._finish_flip(_drain_flip(self.fd))
            try:
                _libdrm.drmModeSetCrtc(self.fd, self.crtc_id, 0, 0, 0, None, 0, None)
            except Exception as e:
                print(f"[drm] blank failed: {e}")
            if self._prev_bo:
                _libgbm.gbm_surface_release_buffer(self.gbm_surface, self._prev_bo)
                self._prev_bo = None

    def teardown(self):
        try:
            for fb in self._fb_for_bo.values():
                _libdrm.drmModeRmFB(self.fd, fb)
            if self.fd is not None:
                os.close(self.fd)
        except Exception:
            pass


# Page-flip events are delivered on the DRM fd; drain one so we pace to vblank.
class _drmEventContext(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_int),
        ("vblank_handler", ctypes.c_void_p),
        ("page_flip_handler", ctypes.c_void_p),
        ("page_flip_handler2", ctypes.c_void_p),
        ("sequence_handler", ctypes.c_void_p),
    ]


_libdrm.drmHandleEvent.argtypes = [ctypes.c_int, ctypes.POINTER(_drmEventContext)]


def _read_flip(fd, timeout):
    import select
    if not select.select([fd], [], [], timeout)[0]:
        return None
    timestamp = []
    callback_type = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_uint,
                                     ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)
    @callback_type
    def flipped(_fd, _sequence, sec, usec, _data):
        timestamp.append(sec * 1_000_000_000 + usec * 1000)
    event = _drmEventContext(version=2, page_flip_handler=ctypes.cast(flipped, ctypes.c_void_p))
    if _libdrm.drmHandleEvent(fd, ctypes.byref(event)):
        raise OSError(ctypes.get_errno(), "drmHandleEvent failed")
    return timestamp[0] if timestamp else None


def _drain_flip(fd):
    deadline = time.monotonic() + .25
    while time.monotonic() < deadline:
        timestamp = _read_flip(fd, max(0, deadline - time.monotonic()))
        if timestamp is not None:
            return timestamp
    raise TimeoutError("DRM page flip did not complete within 250ms")
