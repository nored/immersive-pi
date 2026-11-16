"""Repeat compressed H.264 access units without resetting the hardware decoder.

Only the lightweight MP4 demux/parser is reopened. The decoder sees a single
continuous stream with monotonic PTS/DTS and repeated SPS/PPS at each IDR.
Compressed buffers and decoded frames both apply bounded backpressure.
"""
import threading

from gi.repository import Gst


class EncodedStream:
    def __init__(self, path, loop, generation_valid, on_error, trace=None):
        self._stopped = threading.Event()
        self._thread = None
        self.path, self.loop = path, loop
        self.valid, self.on_error, self.trace = generation_valid, on_error, trace
        self.reader, self.sink = self._open()
        first = self.sink.emit('try-pull-sample', 10 * Gst.SECOND)
        if first is None:
            self.reader.set_state(Gst.State.NULL)
            raise RuntimeError('could not read H.264 access units from MP4')
        self.first = first
        ok, self.duration = self.reader.query_duration(Gst.Format.TIME)
        if not ok or self.duration <= 0:
            self.reader.set_state(Gst.State.NULL)
            raise RuntimeError('could not determine encoded video duration')
        self.caps = first.get_caps().copy()
        self.source = Gst.ElementFactory.make('appsrc', 'encoded-frames')
        self.source.set_property('caps', self.caps)
        self.source.set_property('format', Gst.Format.TIME)
        self.source.set_property('duration', self.duration)
        self.source.set_property('block', True)
        self.source.set_property('max-bytes', 2 * 1024 * 1024)
        # Small, compressible clips can fit many loops into a byte-only queue.
        # Bound access units and media time too, so repeated demux startup does
        # not compete with clocked presentation while decode-ahead is full.
        self.source.set_property('max-buffers', 32)
        self.source.set_property('max-time', Gst.SECOND)

    def _open(self):
        pipeline = Gst.Pipeline.new('encoded-input')
        src = Gst.ElementFactory.make('filesrc', None)
        src.set_property('location', self.path)
        demux = Gst.ElementFactory.make('qtdemux', None)
        parser = Gst.ElementFactory.make('h264parse', None)
        parser.set_property('config-interval', -1)
        caps = Gst.ElementFactory.make('capsfilter', None)
        caps.set_property('caps', Gst.Caps.from_string('video/x-h264,stream-format=byte-stream,alignment=au'))
        sink = Gst.ElementFactory.make('appsink', None)
        sink.set_property('sync', False)
        sink.set_property('max-buffers', 2)
        sink.set_property('drop', False)
        for e in (src, demux, parser, caps, sink):
            pipeline.add(e)
        if not src.link(demux) or not parser.link(caps) or not caps.link(sink):
            raise RuntimeError('could not link compressed input pipeline')
        def added(_demux, pad):
            if pad.query_caps(None).to_string().startswith('video/x-h264'):
                pad.link(parser.get_static_pad('sink'))
        demux.connect('pad-added', added)
        pipeline.set_state(Gst.State.PLAYING)
        return pipeline, sink

    def start(self):
        self._thread = threading.Thread(target=self._feed, daemon=True)
        self._thread.start()

    def close(self):
        self._stopped.set()
        self.reader.set_state(Gst.State.NULL)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(2)

    def _feed(self):
        offset, loops = 0, 0
        sample = self.first
        self.first = None
        try:
            while self.valid() and not self._stopped.is_set():
                if sample is None:
                    sample = self.sink.emit('try-pull-sample', Gst.SECOND // 10)
                if sample is None:
                    message = self.reader.get_bus().pop_filtered(Gst.MessageType.ERROR)
                    if message:
                        raise RuntimeError(str(message.parse_error()[0]))
                    if not self.sink.get_property('eos'):
                        continue
                    if not self.loop:
                        self.source.emit('end-of-stream')
                        return
                    self.reader.set_state(Gst.State.NULL)
                    self.reader, self.sink = self._open()
                    offset += self.duration
                    loops += 1
                    if self.trace:
                        self.trace.record('encoded_loop', loop=loops)
                    continue
                original = sample.get_buffer()
                buffer = original.copy()  # shallow data reference, independent timestamps
                if buffer.pts != Gst.CLOCK_TIME_NONE:
                    buffer.pts += offset
                if buffer.dts != Gst.CLOCK_TIME_NONE:
                    buffer.dts += offset
                if self.source.emit('push-buffer', buffer) != Gst.FlowReturn.OK:
                    return
                sample = None
        except Exception as exc:
            if self.valid():
                self.on_error(str(exc))
        finally:
            self.reader.set_state(Gst.State.NULL)
