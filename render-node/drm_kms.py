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
import os
import struct

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

    def __init__(self, card="/dev/dri/card0", connector_pref="auto"):
        self.card = card
        self.connector_pref = connector_pref
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
            if c.connection == DRM_MODE_CONNECTED and c.count_modes > 0:
                conn = c
                break
        if conn is None:
            raise RuntimeError("no connected connector with a mode")
        self.connector_id = conn.connector_id
        self.mode = conn.modes[0]  # preferred mode is first
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
        _libegl.eglMakeCurrent(self.egl_dpy, self.egl_surf, self.egl_surf, self.egl_ctx)

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

    def swap_and_flip(self):
        _libegl.eglSwapBuffers(self.egl_dpy, self.egl_surf)
        bo = _libgbm.gbm_surface_lock_front_buffer(self.gbm_surface)
        if not bo:
            raise RuntimeError("gbm_surface_lock_front_buffer failed")
        fb = self._fb_for(bo)
        if self._prev_bo is None:
            # first frame: full modeset
            mode_ptr = ctypes.pointer(self.mode)
            conn = (ctypes.c_uint32 * 1)(self.connector_id)
            _libdrm.drmModeSetCrtc(self.fd, self.crtc_id, fb, 0, 0, conn, 1, mode_ptr)
        else:
            # steady state: page flip on vblank, then reclaim the old buffer
            _libdrm.drmModePageFlip(self.fd, self.crtc_id, fb,
                                    DRM_MODE_PAGE_FLIP_EVENT, None)
            _drain_flip(self.fd)
            _libgbm.gbm_surface_release_buffer(self.gbm_surface, self._prev_bo)
        self._prev_bo = bo

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


def _drain_flip(fd):
    import select
    r, _, _ = select.select([fd], [], [], 0.1)
    if fd in r:
        ev = _drmEventContext(version=2)
        _libdrm.drmHandleEvent(fd, ctypes.byref(ev))
