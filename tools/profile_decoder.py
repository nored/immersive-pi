"""Opt-in source instrumentation for the offscreen decoder benchmark only."""
from contextlib import contextmanager
import inspect
import textwrap
import time
import types


def install(player):
    """Instrument the current callback without changing deployed Player code.

    Exact replacements fail closed when the callback changes. Explicit clocks
    avoid sharing a cProfile object across GStreamer callback threads.
    """
    source = textwrap.dedent(inspect.getsource(type(player)._on_sample))
    samples = {}

    @contextmanager
    def measure(name):
        wall, cpu = time.monotonic_ns(), time.thread_time_ns()
        try:
            yield
        finally:
            cpu = (time.thread_time_ns() - cpu) / 1e6
            wall = (time.monotonic_ns() - wall) / 1e6
            values = samples.setdefault(name, {'wall': [], 'cpu': []})
            values['cpu'].append(cpu)
            values['wall'].append(wall)

    for name, statement in (
        ('pull_sample', 'sample = sink.emit("pull-sample")'),
        ('caps_info', 'info = GstVideo.VideoInfo.new_from_caps(caps)'),
        ('video_meta', 'metadata = GstVideo.buffer_get_video_meta(buf)'),
        ('map', 'ok, mapped = buf.map(Gst.MapFlags.READ)'),
        ('unmap', 'buf.unmap(mapped)'),
    ):
        lines = source.splitlines(keepends=True)
        indices = [i for i, line in enumerate(lines) if line.strip() == statement]
        if len(indices) != 1:
            raise RuntimeError(f'Decoder profiler cannot locate {name}')
        i = indices[0]
        indent = lines[i][:-len(lines[i].lstrip())]
        lines[i] = f'{indent}with _measure_decode({name!r}):\n{indent}    {statement}\n'
        source = ''.join(lines)
    old = '''            array = np.ndarray((height, info.width), dtype=np.uint8,
                               buffer=mapped.data, offset=offsets[plane],
                               strides=(strides[plane], 1))'''
    new = '''            with _measure_decode(f'plane_{plane}_data'):
                data = mapped.data
            with _measure_decode(f'plane_{plane}_view'):
                array = np.ndarray((height, info.width), dtype=np.uint8,
                                   buffer=data, offset=offsets[plane],
                                   strides=(strides[plane], 1))'''
    if source.count(old) != 1:
        raise RuntimeError('Decoder profiler cannot locate plane packing')
    source = source.replace(old, new)
    old_copy = '            planes.append(array.tobytes())'
    new_copy = '            planes.append(array.copy(order="C").tobytes())'
    if source.count(new_copy) == 1:
        source = source.replace(new_copy, '''            with _measure_decode(f'plane_{plane}_read'):
                owned = array.copy(order="C")
            with _measure_decode(f'plane_{plane}_copy'):
                planes.append(owned.tobytes())''')
    elif source.count(old_copy) == 1:
        source = source.replace(old_copy, '''            with _measure_decode(f'plane_{plane}_copy'):
                planes.append(array.tobytes())''')
    else:
        raise RuntimeError('Decoder profiler cannot locate plane copy')
    namespace = dict(type(player)._on_sample.__globals__, _measure_decode=measure)
    exec(compile(source, '<decoder-benchmark-profile>', 'exec'), namespace)
    player._on_sample = types.MethodType(namespace['_on_sample'], player)
    return samples
