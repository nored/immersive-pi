"""gl_pipeline.py — EGL over GBM/DRM, GLES2, texture -> NxM mesh warp ->
blend+color -> KMS scanout, plus a preview FBO read back for the control node.

Two pieces:
  * KmsDisplay  — owns the DRM connector/CRTC and a GBM surface, drives EGL, and
    page-flips each rendered frame straight to one projector. We deliberately do
    NOT use kmssink: it would own the display and leave no room for this custom
    GL stage.
  * GLStage     — compiles warp.vert + blend.frag, tessellates the room-model
    mesh into a triangle grid, uploads each decoded frame to a texture, applies
    blend and color in the fragment shader, and renders both to the screen and
    to a small preview FBO that the agent reads back, JPEG-encodes, and streams.

Hardware bring-up (DRM master, GBM, EGL context) is lazy so this module imports
and the GL logic is reviewable on a non-Pi box; the KMS path only runs on the
target. Confirming GLES2-over-GBM/DRM headless on the chosen board is an
explicit Milestone-1 task (Pi 4 vs Pi 5).
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Optional

SHADER_DIR = Path(__file__).with_name("shaders")

PATTERN_IDS = {"video": 0, "grid": 1, "crosshair": 2, "grey": 3, "white": 4, "color": 5}


# ----------------------------------------------------------------------------
# Mesh tessellation — turns a room-model mesh entry into interleaved vertex data
# matching warp.vert's attributes: a_pos(x,y), a_uv(u,v), a_edge(u,v).
# ----------------------------------------------------------------------------
def build_mesh_arrays(entry: dict):
    import numpy as np
    mesh = entry["mesh"]
    region = entry.get("source_region", {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0})
    cols, rows = mesh["cols"], mesh["rows"]
    pts = mesh["points"]
    verts = []
    for p in pts:
        u, v = p["u"], p["v"]
        # source coord = control-point uv mapped into this node's source_region
        su = region["x"] + u * region["w"]
        sv = region["y"] + v * region["h"]
        # a_pos, a_uv, a_edge(=normalized output coord, the grid param)
        verts.extend([p["x"], p["y"], su, sv, u, v])
    verts = np.asarray(verts, dtype=np.float32)

    idx = []
    for r in range(rows - 1):
        for c in range(cols - 1):
            i00 = r * cols + c
            i10 = i00 + 1
            i01 = i00 + cols
            i11 = i01 + 1
            idx.extend([i00, i10, i11, i00, i11, i01])
    idx = np.asarray(idx, dtype=np.uint16)
    return verts, idx


# ----------------------------------------------------------------------------
# KMS display via GBM/DRM/EGL (ctypes). Real path; only used on the Pi.
# ----------------------------------------------------------------------------
class KmsDisplay:
    def __init__(self, card: str = "/dev/dri/card0", connector: str = "auto"):
        self.card = card
        self.connector_pref = connector
        self.width = 0
        self.height = 0
        self._ready = False
        # handles filled by setup()
        self._drm_fd = None
        self._gbm = None
        self._egl = None

    def setup(self):
        """Open DRM, pick connector+mode, create GBM surface + EGL context.
        Imports the native libs lazily so this only runs on the target."""
        import drm_kms  # local thin ctypes wrapper (see drm_kms.py)
        self._backend = drm_kms.Backend(self.card, self.connector_pref)
        self.width, self.height = self._backend.modeset()
        self._backend.egl_init()
        self._ready = True
        return self.width, self.height

    def make_current(self):
        self._backend.make_current()

    def swap_and_flip(self):
        """Swap EGL buffers and page-flip the new front buffer to the CRTC."""
        self._backend.swap_and_flip()

    def teardown(self):
        if self._ready:
            self._backend.teardown()
            self._ready = False


# ----------------------------------------------------------------------------
# GL stage — programs, mesh, uniforms, texture, preview FBO.
# ----------------------------------------------------------------------------
class GLStage:
    def __init__(self, width: int, height: int, preview_w: int = 320, preview_h: int = 180):
        self.width = width
        self.height = height
        self.preview_w = preview_w
        self.preview_h = preview_h
        self.prog = None
        self.raw_prog = None       # passthrough for structured-light scan frames
        self.raw_vbo = None
        self.tex = None
        self.vbo = None
        self.ibo = None
        self.index_count = 0
        self.preview_fbo = None
        self.preview_tex = None
        self._loc = {}
        self._tex_w = 0
        self._tex_h = 0
        self._entry = None
        self._pattern = 0
        self._pattern_color = (1.0, 0.0, 0.0)

    # ---- GL setup --------------------------------------------------------
    def init_gl(self):
        from OpenGL import GLES2 as gl
        self.gl = gl
        vs = (SHADER_DIR / "warp.vert").read_text()
        fs = (SHADER_DIR / "blend.frag").read_text()
        self.prog = self._link(vs, fs)
        self.tex = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        self.vbo = gl.glGenBuffers(1)
        self.ibo = gl.glGenBuffers(1)
        self._setup_preview_fbo()
        self._setup_raw_passthrough()
        self._cache_locations()

    def _setup_raw_passthrough(self):
        """A trivial fullscreen textured quad used to scan structured-light
        patterns out raw — no mesh warp, no blend, no colour trim."""
        import numpy as np
        gl = self.gl
        vs = ("attribute vec2 a_pos; attribute vec2 a_uv; varying vec2 v_uv;"
              "void main(){ v_uv=a_uv; gl_Position=vec4(a_pos,0.0,1.0); }")
        fs = ("precision mediump float; varying vec2 v_uv; uniform sampler2D u_tex;"
              "void main(){ gl_FragColor = texture2D(u_tex, v_uv); }")
        self.raw_prog = self._link(vs, fs)
        gl.glBindAttribLocation(self.raw_prog, 0, "a_pos")
        gl.glBindAttribLocation(self.raw_prog, 1, "a_uv")
        # two triangles covering clip space; uv flipped so the uploaded image
        # (row 0 = top) appears upright on the projector
        quad = np.array([
            -1, -1, 0, 1,   1, -1, 1, 1,   1, 1, 1, 0,
            -1, -1, 0, 1,   1, 1, 1, 0,   -1, 1, 0, 0,
        ], dtype=np.float32)
        self.raw_vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.raw_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, quad.nbytes, quad, gl.GL_STATIC_DRAW)

    def _link(self, vs_src, fs_src):
        gl = self.gl
        def compile(src, kind):
            s = gl.glCreateShader(kind)
            gl.glShaderSource(s, src)
            gl.glCompileShader(s)
            if gl.glGetShaderiv(s, gl.GL_COMPILE_STATUS) != gl.GL_TRUE:
                raise RuntimeError(gl.glGetShaderInfoLog(s).decode())
            return s
        v = compile(vs_src, gl.GL_VERTEX_SHADER)
        f = compile(fs_src, gl.GL_FRAGMENT_SHADER)
        p = gl.glCreateProgram()
        gl.glAttachShader(p, v)
        gl.glAttachShader(p, f)
        gl.glBindAttribLocation(p, 0, "a_pos")
        gl.glBindAttribLocation(p, 1, "a_uv")
        gl.glBindAttribLocation(p, 2, "a_edge")
        gl.glLinkProgram(p)
        if gl.glGetProgramiv(p, gl.GL_LINK_STATUS) != gl.GL_TRUE:
            raise RuntimeError(gl.glGetProgramInfoLog(p).decode())
        return p

    def _cache_locations(self):
        gl = self.gl
        for name in ("u_tex", "u_blend_left", "u_blend_right", "u_blend_top",
                     "u_blend_bottom", "u_black_lift", "u_gain", "u_gamma",
                     "u_lift", "u_pattern", "u_pattern_color", "u_tex_size"):
            self._loc[name] = gl.glGetUniformLocation(self.prog, name)

    def _setup_preview_fbo(self):
        gl = self.gl
        self.preview_tex = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.preview_tex)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, self.preview_w, self.preview_h,
                        0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        self.preview_fbo = gl.glGenFramebuffers(1)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.preview_fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, self.preview_tex, 0)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)

    # ---- live parameter updates -----------------------------------------
    def set_entry(self, entry: dict):
        """(Re)build the mesh VBO and stash blend/color for the next draw.
        Called on connect and on every set_mesh/set_blend/set_color."""
        import numpy as np
        gl = self.gl
        self._entry = entry
        verts, idx = build_mesh_arrays(entry)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, verts.nbytes, verts, gl.GL_DYNAMIC_DRAW)
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, self.ibo)
        gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, idx.nbytes, idx, gl.GL_DYNAMIC_DRAW)
        self.index_count = len(idx)

    def set_pattern(self, kind: str, on: bool, color=(1.0, 0.0, 0.0)):
        self._pattern = PATTERN_IDS.get(kind, 0) if on else 0
        self._pattern_color = color

    # ---- frame upload + draw --------------------------------------------
    def upload_frame(self, frame):
        gl = self.gl
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        if frame.width != self._tex_w or frame.height != self._tex_h:
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, frame.width, frame.height,
                            0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, frame.data)
            self._tex_w, self._tex_h = frame.width, frame.height
        else:
            gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, frame.width, frame.height,
                               gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, frame.data)

    def _apply_uniforms(self):
        gl = self.gl
        e = self._entry or {}
        blend = e.get("blend", {})
        color = e.get("color", {"gain": [1, 1, 1], "gamma": 2.2, "lift": [0, 0, 0]})

        def edge(name):
            b = blend.get(name, {"width": 0.0, "gamma": 2.2})
            return float(b.get("width", 0.0)), float(b.get("gamma", 2.2))

        def lift(name):
            return float(blend.get(name, {}).get("black_lift", 0.0))

        gl.glUniform1i(self._loc["u_tex"], 0)
        gl.glUniform2f(self._loc["u_blend_left"], *edge("left"))
        gl.glUniform2f(self._loc["u_blend_right"], *edge("right"))
        gl.glUniform2f(self._loc["u_blend_top"], *edge("top"))
        gl.glUniform2f(self._loc["u_blend_bottom"], *edge("bottom"))
        gl.glUniform4f(self._loc["u_black_lift"], lift("left"), lift("right"),
                       lift("top"), lift("bottom"))
        gain = color.get("gain", [1, 1, 1])
        gl.glUniform3f(self._loc["u_gain"], *gain)
        gl.glUniform1f(self._loc["u_gamma"], float(color.get("gamma", 2.2)))
        gl.glUniform3f(self._loc["u_lift"], *color.get("lift", [0, 0, 0]))
        gl.glUniform1i(self._loc["u_pattern"], self._pattern)
        gl.glUniform3f(self._loc["u_pattern_color"], *self._pattern_color)
        gl.glUniform2f(self._loc["u_tex_size"], float(self._tex_w or 1), float(self._tex_h or 1))

    def _draw_geometry(self):
        gl = self.gl
        gl.glUseProgram(self.prog)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        self._apply_uniforms()
        stride = 6 * 4
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(1, 2, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(8))
        gl.glEnableVertexAttribArray(2)
        gl.glVertexAttribPointer(2, 2, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(16))
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, self.ibo)
        gl.glDrawElements(gl.GL_TRIANGLES, self.index_count, gl.GL_UNSIGNED_SHORT,
                          ctypes.c_void_p(0))

    def render_screen(self):
        gl = self.gl
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        gl.glViewport(0, 0, self.width, self.height)
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        if self.index_count:
            self._draw_geometry()

    def upload_raw(self, rgba: bytes, w: int, h: int):
        """Upload an arbitrary RGBA image (a scan pattern) into the texture."""
        gl = self.gl
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, w, h, 0,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, rgba)
        self._tex_w, self._tex_h = w, h

    def render_raw(self):
        """Draw the current texture fullscreen with no warp/blend — scan mode."""
        gl = self.gl
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        gl.glViewport(0, 0, self.width, self.height)
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        gl.glUseProgram(self.raw_prog)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        gl.glUniform1i(gl.glGetUniformLocation(self.raw_prog, "u_tex"), 0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.raw_vbo)
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 16, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(1, 2, gl.GL_FLOAT, gl.GL_FALSE, 16, ctypes.c_void_p(8))
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6)

    def render_preview(self) -> Optional[bytes]:
        """Render to the small FBO and read RGBA back for JPEG encoding."""
        gl = self.gl
        if not self.index_count:
            return None
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.preview_fbo)
        gl.glViewport(0, 0, self.preview_w, self.preview_h)
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        self._draw_geometry()
        data = gl.glReadPixels(0, 0, self.preview_w, self.preview_h,
                               gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
        return bytes(data)
