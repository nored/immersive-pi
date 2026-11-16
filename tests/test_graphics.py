import ctypes
import copy

import pytest


@pytest.fixture
def egl_context():
    from OpenGL import EGL
    display = EGL.eglGetDisplay(EGL.EGL_DEFAULT_DISPLAY)
    major, minor = EGL.EGLint(), EGL.EGLint()
    assert EGL.eglInitialize(display, major, minor)
    assert EGL.eglBindAPI(EGL.EGL_OPENGL_ES_API)
    attribs = (EGL.EGLint * 11)(
        EGL.EGL_SURFACE_TYPE, EGL.EGL_PBUFFER_BIT,
        EGL.EGL_RENDERABLE_TYPE, EGL.EGL_OPENGL_ES2_BIT,
        EGL.EGL_RED_SIZE, 8, EGL.EGL_GREEN_SIZE, 8,
        EGL.EGL_BLUE_SIZE, 8, EGL.EGL_NONE)
    configs = (EGL.EGLConfig * 1)()
    count = EGL.EGLint()
    assert EGL.eglChooseConfig(display, attribs, configs, 1, count)
    context = EGL.eglCreateContext(display, configs[0], EGL.EGL_NO_CONTEXT,
                                  (EGL.EGLint * 3)(EGL.EGL_CONTEXT_CLIENT_VERSION, 2, EGL.EGL_NONE))
    surface = EGL.eglCreatePbufferSurface(display, configs[0],
        (EGL.EGLint * 5)(EGL.EGL_WIDTH, 64, EGL.EGL_HEIGHT, 32, EGL.EGL_NONE))
    assert EGL.eglMakeCurrent(display, surface, surface, context)
    yield
    EGL.eglMakeCurrent(display, EGL.EGL_NO_SURFACE, EGL.EGL_NO_SURFACE, EGL.EGL_NO_CONTEXT)
    EGL.eglDestroySurface(display, surface)
    EGL.eglDestroyContext(display, context)
    EGL.eglTerminate(display)


def test_diagnostic_readback_is_allocated_only_on_explicit_request(egl_context):
    from gl_pipeline import GLStage
    from roommodel import default_node_entry
    from PIL import Image
    import io
    stage = GLStage(64, 32, 32, 16)
    stage.init_gl()
    assert stage.preview_fbo is None
    stage.set_entry(default_node_entry("test"))
    stage.set_pattern("color", True, (1, 0, 0))
    pixels = stage.render_preview()
    assert len(pixels) == 32 * 16 * 4
    image = Image.frombytes("RGBA", (32, 16), pixels).convert("RGB")
    r, g, b = image.getpixel((16, 8))
    assert r > 240 and g < 10 and b < 10


@pytest.mark.parametrize("matrix,full_range", [("bt709", False), ("bt601", False), ("bt709", True)])
def test_nv12_shader_converts_black_and_white_correctly(egl_context, matrix, full_range):
    from gl_pipeline import GLStage
    from roommodel import default_node_entry
    from player import Frame
    stage = GLStage(64, 32, 32, 16)
    stage.init_gl()
    entry = default_node_entry("test")
    entry["color"]["gamma"] = 1
    stage.set_entry(entry)
    for luma, expected in ((0 if full_range else 16, 0), (255 if full_range else 235, 255)):
        stage.upload_frame(Frame((bytes([luma]) * (64 * 32), bytes([128]) * (64 * 16)),
                                 64, 32, 0, 0, matrix=matrix, full_range=full_range))
        pixels = stage.render_preview()
        center = (8 * 32 + 16) * 4
        assert all(abs(c - expected) <= 2 for c in pixels[center:center + 3])


@pytest.mark.parametrize('projector_gamma', [1.0, 2.2, 2.6])
def test_complementary_projector_edges_preserve_total_light(egl_context, projector_gamma):
    from gl_pipeline import GLStage
    from roommodel import default_node_entry
    stage = GLStage(64, 32, 64, 32)
    stage.init_gl()
    samples = []
    for edge in ('left', 'right'):
        entry = default_node_entry('test')
        entry['color']['gamma'] = 1
        entry['blend'][edge] = {'width': 1, 'gamma': projector_gamma, 'black_lift': 0}
        stage.set_entry(entry)
        stage.set_pattern('white', True)
        pixels = stage.render_preview()
        samples.append([pixels[(16 * 64 + x) * 4] / 255 for x in range(64)])
    # Two projectors illuminate the same surface. Light adds after each
    # projector's transfer function, rather than by adding encoded RGB bytes.
    for left, right in zip(*samples):
        assert abs(left ** projector_gamma + right ** projector_gamma - 1) < .025


def test_default_colour_trim_preserves_encoded_midtones(egl_context):
    from gl_pipeline import GLStage
    from roommodel import default_node_entry
    stage = GLStage(64, 32, 32, 16)
    stage.init_gl()
    stage.set_entry(default_node_entry('test'))
    stage.set_pattern('color', True, (.5, .25, .75))
    pixels = stage.render_preview()
    center = (8 * 32 + 16) * 4
    assert all(abs(actual - expected) <= 2 for actual, expected in
               zip(pixels[center:center + 3], (128, 64, 191)))


def test_phone_scan_raw_target_preserves_pixels_orientation_and_ignores_warp_trim(egl_context):
    import numpy as np
    from gl_pipeline import GLStage
    from roommodel import default_node_entry
    stage=GLStage(64,32)
    stage.init_gl()
    entry=default_node_entry('scan-test')
    entry['color']['gain']=[.1,.2,.3]
    entry['color']['gamma']=2.2
    entry['source_region']={'x':.3,'y':.2,'w':.2,'h':.3,'wrap':False}
    entry['blend']['left']['width']=.5
    stage.set_entry(entry)
    pixels=np.empty((32,64,4),dtype=np.uint8)
    pixels[:16,:32]=[255,0,0,255];pixels[:16,32:]=[0,255,0,255]
    pixels[16:,:32]=[0,0,255,255];pixels[16:,32:]=[255,255,0,255]
    stage.upload_raw(pixels.tobytes(),64,32)
    stage.render_raw();stage.gl.glFinish()
    raw=(ctypes.c_ubyte*(64*32*4))()
    stage.gl.glReadPixels(0,0,64,32,stage.gl.GL_RGBA,stage.gl.GL_UNSIGNED_BYTE,raw)
    observed=np.frombuffer(raw,dtype=np.uint8).reshape(32,64,4)[::-1]
    assert np.max(np.abs(observed.astype(int)-pixels.astype(int)))<=1


@pytest.mark.parametrize('tile', [None, {'x':.03125,'y':0,'w':.25,'h':1},
                                       {'x':.5,'y':0,'w':.125,'h':1}])
def test_source_grid_is_independent_of_cached_video_tile_coordinates(egl_context, tile):
    from gl_pipeline import GLStage
    from roommodel import default_node_entry
    stage = GLStage(128,64,128,64)
    stage.init_gl()
    entry = default_node_entry('test')
    entry['source_region'] = {'x':.03125,'y':0,'w':.25,'h':1,'wrap':True}
    stage.set_entry(entry)
    stage.set_pattern('source-grid', True)
    expected = stage.render_preview()
    # Even a stopped layout draft outside the cached tile can be aligned with
    # this procedural pattern: it never reads unavailable image pixels.
    stage.set_video_region(tile)
    assert stage.render_preview() == expected


@pytest.mark.parametrize('left_x,right_x', [(.03125,.15625), (.90625,.03125)])
def test_source_grid_marks_match_in_overlap_and_across_360_degree_seam(egl_context, left_x, right_x):
    import numpy as np
    from gl_pipeline import GLStage
    from roommodel import default_node_entry
    stage = GLStage(128,64,128,64)
    stage.init_gl()
    samples = []
    for x in (left_x,right_x):
        entry = default_node_entry('test')
        entry['source_region'] = {'x':x,'y':0,'w':.25,'h':1,'wrap':True}
        stage.set_entry(entry)
        stage.set_video_region(copy.deepcopy(entry['source_region']))
        stage.set_pattern('source-grid',True)
        samples.append(np.frombuffer(stage.render_preview(), dtype=np.uint8).reshape(64,128,4))
    assert np.array_equal(samples[0][:,64:],samples[1][:,:64])




@pytest.mark.parametrize('rgb',[(0,0,0),(255,255,255),(255,0,0)])
def test_scan_framebuffer_samples_distinguish_gray_from_red(egl_context,rgb):
    from gl_pipeline import GLStage
    stage=GLStage(64,32)
    stage.init_gl()
    stage.upload_raw(bytes((*rgb,255))*(64*32),64,32)
    stage.render_raw()
    result=stage.sample_scan_rgb()
    assert result['sample_count']==9
    assert all(all(abs(a-b)<=1 for a,b in zip(pixel,rgb)) for pixel in result['rgb'])
    assert abs(result['max_channel_spread']-(max(rgb)-min(rgb)))<=1
    assert result['optical_output_verified'] is False
