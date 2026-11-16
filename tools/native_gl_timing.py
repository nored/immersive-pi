"""Optional benchmark-only timing inside GL calls, before Python reacquires GIL."""
import ctypes
import time


def install(gl, library):
    if getattr(gl, '_immersive_native_timing', False):
        raise RuntimeError('Native GL timing is already installed in this process')
    native = ctypes.CDLL(str(library))
    texture_operation = gl.glTexSubImage2D.wrappedOperation
    texture = texture_operation.load()
    finish_operation = gl.glFinish
    finish = finish_operation.load()
    native.set_functions.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    native.set_functions.restype = None
    native.set_functions(ctypes.cast(texture, ctypes.c_void_p), ctypes.cast(finish, ctypes.c_void_p))
    for replacement, original, metadata in ((native.timed_texture, texture, texture_operation),
                                             (native.timed_finish, finish, finish_operation)):
        replacement.argtypes = original.argtypes
        replacement.restype = original.restype
        if original.errcheck is not None:
            replacement.errcheck = original.errcheck
        replacement.argNames = metadata.argNames
    metrics = {name: ctypes.c_uint64.in_dll(native, name) for name in
               ('texture_wall_ns', 'texture_cpu_ns', 'finish_wall_ns', 'finish_cpu_ns')}
    samples = {name: [] for name in ('texture_native_wall_ms', 'texture_native_cpu_ms',
                                    'texture_python_wall_ms', 'finish_native_wall_ms',
                                    'finish_native_cpu_ms', 'finish_python_wall_ms')}
    # Keep the ordinary PyOpenGL argument conversion and error checking intact.
    gl.glTexSubImage2D.wrappedOperation = native.timed_texture
    texture_wrapper = gl.glTexSubImage2D
    def timed_texture(*args, **kwargs):
        started = time.monotonic_ns()
        value = texture_wrapper(*args, **kwargs)
        samples['texture_python_wall_ms'].append((time.monotonic_ns() - started) / 1e6)
        samples['texture_native_wall_ms'].append(metrics['texture_wall_ns'].value / 1e6)
        samples['texture_native_cpu_ms'].append(metrics['texture_cpu_ns'].value / 1e6)
        return value
    def timed_finish(*args, **kwargs):
        started = time.monotonic_ns()
        value = native.timed_finish(*args, **kwargs)
        samples['finish_python_wall_ms'].append((time.monotonic_ns() - started) / 1e6)
        samples['finish_native_wall_ms'].append(metrics['finish_wall_ns'].value / 1e6)
        samples['finish_native_cpu_ms'].append(metrics['finish_cpu_ns'].value / 1e6)
        return value
    gl.glTexSubImage2D, gl.glFinish = timed_texture, timed_finish
    gl._immersive_native_timing = (native, texture_operation, finish_operation)
    return samples
