#!/usr/bin/env python3
"""Measure decode + GLES capacity without KMS modesets, scanout or WebSockets.

Use an idle Pi with its normal renderer stopped to avoid GPU contention.
This is a capacity measurement, not proof of synchronized fleet presentation.
"""
import argparse
import ctypes
import gc
import json
import os
from pathlib import Path
import statistics
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--app-dir', type=Path, required=True)
    parser.add_argument('--frames', type=int, default=1440)
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=800)
    parser.add_argument('--decoder', default='v4l2h264dec')
    parser.add_argument('--native-timing-lib', type=Path)
    parser.add_argument('--profile-components', action='store_true')
    parser.add_argument('--profile-decoder', action='store_true',
                        help='Profile decoded-frame callback using its thread CPU clock')
    parser.add_argument('--replay-frames', type=int, default=0,
                        help='Predecode this many frames, stop decoder, then cycle them to isolate decode contention')
    parser.add_argument('--python-switch-ms', type=float)
    parser.add_argument('--gl-error-checks', choices=('per-call','per-frame'), default='per-call')
    args = parser.parse_args()
    if args.replay_frames < 0 or args.replay_frames > 64:
        parser.error('Replay frame count must be between 0 and 64')
    if args.python_switch_ms is not None:
        if args.python_switch_ms <= 0:
            parser.error('Python switch interval must be positive')
        sys.setswitchinterval(args.python_switch_ms / 1000)
    os.environ['PYOPENGL_PLATFORM'] = 'egl'
    os.environ['EGL_PLATFORM'] = 'surfaceless'
    sys.path[:0] = [str(args.app_dir/'render-node'), str(args.app_dir/'control-node')]
    # This PyOpenGL EGL release requires its error checker during import.
    # Configure GLES checking only after EGL has initialized its bindings.
    from OpenGL import EGL
    if args.gl_error_checks == 'per-frame':
        import OpenGL
        from OpenGL import _configflags
        OpenGL.ERROR_CHECKING = False
        # EGL imported the configuration snapshot already; update it too.
        _configflags.ERROR_CHECKING = False
    from gl_pipeline import GLStage
    from player import Player
    from roommodel import default_node_entry
    display = EGL.eglGetDisplay(EGL.EGL_DEFAULT_DISPLAY)
    major, minor = EGL.EGLint(), EGL.EGLint()
    if not EGL.eglInitialize(display, major, minor):
        raise RuntimeError('Cannot initialize offscreen EGL')
    context = surface = None
    player = Player(decoder=args.decoder, buffer_frames=max(24, args.replay_frames))
    decoder_profile = None
    if args.profile_decoder:
        from profile_decoder import install
        decoder_profile = install(player)
    try:
        assert EGL.eglBindAPI(EGL.EGL_OPENGL_ES_API)
        attrs = (EGL.EGLint * 11)(EGL.EGL_SURFACE_TYPE, EGL.EGL_PBUFFER_BIT,
            EGL.EGL_RENDERABLE_TYPE, EGL.EGL_OPENGL_ES2_BIT,
            EGL.EGL_RED_SIZE, 8, EGL.EGL_GREEN_SIZE, 8,
            EGL.EGL_BLUE_SIZE, 8, EGL.EGL_NONE)
        configs, count = (EGL.EGLConfig * 1)(), EGL.EGLint()
        assert EGL.eglChooseConfig(display, attrs, configs, 1, count) and count.value
        context = EGL.eglCreateContext(display, configs[0], EGL.EGL_NO_CONTEXT,
            (EGL.EGLint * 3)(EGL.EGL_CONTEXT_CLIENT_VERSION, 2, EGL.EGL_NONE))
        surface = EGL.eglCreatePbufferSurface(display, configs[0],
            (EGL.EGLint * 5)(EGL.EGL_WIDTH, args.width, EGL.EGL_HEIGHT, args.height, EGL.EGL_NONE))
        assert EGL.eglMakeCurrent(display, surface, surface, context)
        stage = GLStage(args.width, args.height)
        stage.init_gl()
        native_samples = None
        if args.native_timing_lib:
            from native_gl_timing import install
            native_samples = install(stage.gl, args.native_timing_lib)
        components = {}
        if args.profile_components:
            def timed(name, function):
                samples = components.setdefault(name, {'wall': [], 'cpu': []})
                def call(*a, **kw):
                    wall, cpu = time.monotonic_ns(), time.thread_time_ns()
                    result = function(*a, **kw)
                    samples['cpu'].append((time.thread_time_ns() - cpu) / 1e6)
                    samples['wall'].append((time.monotonic_ns() - wall) / 1e6)
                    return result
                return call
            for name in ('upload_frame', '_apply_uniforms', '_draw_geometry'):
                setattr(stage, name, timed(name, getattr(stage, name)))
            for name in ('glTexSubImage2D', 'glDrawElements', 'glFinish'):
                setattr(stage.gl, name, timed(name, getattr(stage.gl, name)))
        graphics = stage.graphics_info()
        if 'llvmpipe' in graphics['renderer'].lower() or 'softpipe' in graphics['renderer'].lower():
            raise RuntimeError('Software rendering cannot validate Pi GPU capacity')
        entry = default_node_entry('offscreen-benchmark')
        entry['color']['gamma'] = 1
        stage.set_entry(entry)
        spec = player.prepare(str(args.video))
        assert (spec['width'], spec['height']) == (args.width, args.height), spec
        replay = []
        if args.replay_frames:
            count = min(args.replay_frames, spec['frames'])
            replay = [player.take_frame(i) for i in range(count)]
            assert all(frame is not None for frame in replay), 'Missing predecoded frame'
            player.stop()
        gc.collect()
        gc.freeze()
        print(json.dumps({'type':'prepared','graphics':graphics,'spec':spec,'output':[args.width,args.height],
            'gl_error_checks':args.gl_error_checks, 'replay_frames':len(replay)}),flush=True)
        draws, waits = [], []
        sequence, started = 0, time.monotonic()
        while sequence < args.frames:
            if time.monotonic() - started > 150:
                raise TimeoutError('Offscreen benchmark did not finish')
            before = time.monotonic_ns()
            while True:
                if player.error:
                    raise RuntimeError(player.error)
                frame = replay[sequence % len(replay)] if replay else player.take_frame(sequence)
                if frame is not None:
                    break
                if time.monotonic() - started > 150:
                    raise TimeoutError('Decoder did not provide the next frame')
                time.sleep(.0005)
            ready = time.monotonic_ns()
            stage.draw_frame(frame)
            if args.gl_error_checks == 'per-frame':
                error = stage.gl.glGetError()
                if error != stage.gl.GL_NO_ERROR:
                    raise RuntimeError(f'GL frame failed: {error:#x}')
            draws.append((time.monotonic_ns()-ready)/1e6)
            waits.append((ready-before)/1e6)
            player.mark_presented(frame)
            sequence += 1
            if sequence % 360 == 0:
                print(json.dumps({'type':'progress','frames':sequence,'elapsed_s':time.monotonic()-started}),flush=True)
        elapsed = time.monotonic()-started
        if decoder_profile is not None:
            # Join decoder callbacks before reading profiler state.
            player.stop()
        def distribution(values):
            return {'median_ms':statistics.median(values), 'p99_ms':sorted(values)[min(len(values)-1,int(len(values)*.99))], 'max_ms':max(values)}
        if decoder_profile is not None:
            print(json.dumps({'type':'decoder_component_timing','timings':{
                name:{clock:distribution(values) for clock,values in samples.items()}
                for name,samples in decoder_profile.items()},
                'limitation':'Explicit callback stages, including startup; thread CPU and wall clocks; instrumentation adds overhead.'}),flush=True)
        assert stage.preview_fbo is None, 'Unexpected preview allocation'
        if components:
            print(json.dumps({'type':'component_timing', 'timings':{
                name: {clock: distribution(values) for clock, values in samples.items()}
                for name, samples in components.items()},
                'limitation':'Nested timings overlap; CPU is calling thread only; wall minus CPU does not identify the cause of waiting.'}),flush=True)
        if native_samples is not None:
            print(json.dumps({'type':'native_driver_timing',
                'timings':{name:distribution(values) for name,values in native_samples.items()}}),flush=True)
        print(json.dumps({'type':'result','frames':sequence,'elapsed_s':elapsed,'capacity_fps':sequence/elapsed,
            'loops':None if replay else sequence/spec['frames'], 'replay_frames':len(replay),
            'decode_wait':distribution(waits),'gpu_draw':distribution(draws),
            'preview_readbacks':0,'decoder_error':player.error,'decoder_restarts':player.decoder_restarts,
            'limitation':'Offscreen decode and draw only; no native buffer handoff, network barrier or HDMI presentation.'}),flush=True)
    finally:
        player.stop()
        gc.unfreeze()
        EGL.eglMakeCurrent(display, EGL.EGL_NO_SURFACE, EGL.EGL_NO_SURFACE, EGL.EGL_NO_CONTEXT)
        if surface: EGL.eglDestroySurface(display, surface)
        if context: EGL.eglDestroyContext(display, context)
        EGL.eglTerminate(display)


if __name__ == '__main__':
    main()
