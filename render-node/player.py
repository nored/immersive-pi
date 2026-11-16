"""Decode video ahead of presentation; protocol v2 owns frame timing.

GStreamer supplies sequential NV12 frames at decode speed into a bounded queue.
It never seeks periodically to catch up with a wall clock. The master commits
numbered frames only when all nodes have buffered them; the GL thread presents
those frames against a shared GstNet clock. Decoder timing and display timing
are deliberately separate.
"""
from __future__ import annotations

import hashlib
import sys
import threading
import time
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GstNet", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstNet, GstVideo, GLib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.frame_sync import DecodeBuffer
from encoded_stream import EncodedStream

Gst.init(None)


@dataclass(slots=True)
class Frame:
    planes: tuple
    width: int
    height: int
    pts: int
    sequence: int
    format: str = "NV12"
    matrix: str = "bt709"
    full_range: bool = False


class Player:
    def __init__(self, on_eos=None, decoder="v4l2h264dec", buffer_frames=24):
        if decoder not in ("v4l2h264dec", "v4l2slh264dec", "avdec_h264"):
            raise ValueError(f"unsupported decoder: {decoder}")
        self.trace = None
        self.decoder = decoder
        self.buffer_frames = buffer_frames
        self.pipeline = None
        self.appsink = None
        self.net_clock = None
        self.spec = None
        self.error = None
        self._buffer = DecodeBuffer(buffer_frames)
        self._sequence = 0
        self._pts_origin = None
        self._presented = -1
        self._media_pos_ns = 0
        self._decoder_ok = False
        self._loop_media = True
        self._bus = None
        self._glib_loop = GLib.MainLoop()
        self._glib_thread = None
        self._on_eos = on_eos
        self._pipeline_lock = threading.RLock()
        self._stopping = True
        self._generation = 0
        self._media_path = None
        self._encoded = None
        self.decoder_restarts = 0
        self.decoder_restart_ms = None

    def prepare(self, media_path: str, loop=True):
        self.stop()
        self.error = None
        self._loop_media = loop
        self._buffer = DecodeBuffer(self.buffer_frames)
        self._sequence = 0
        self._pts_origin = None
        self._presented = -1
        self._media_pos_ns = 0
        self._stopping = False
        self._media_path = str(media_path)
        self.decoder_restarts = 0
        self.decoder_restart_ms = None
        try:
            pipeline, sink = self._build_decoder(media_path)
            if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError(self.error or "decoder failed to preroll")
            result, state, _pending = pipeline.get_state(10 * Gst.SECOND)
            if result == Gst.StateChangeReturn.FAILURE or state != Gst.State.PAUSED:
                raise RuntimeError(self.error or "decoder preroll timed out after 10s")
            caps = sink.get_static_pad("sink").get_current_caps()
            info = GstVideo.VideoInfo.new_from_caps(caps)
            if info.fps_n <= 0 or info.fps_d <= 0:
                raise RuntimeError("constant-frame-rate video with a known frame rate is required")
            ok, duration = pipeline.query_duration(Gst.Format.TIME)
            if not ok or duration <= 0:
                raise RuntimeError("could not determine video duration")
            frames = round(duration * info.fps_n / (Gst.SECOND * info.fps_d))
            with open(media_path, "rb") as media:
                digest = hashlib.file_digest(media, "sha256").hexdigest()
            self.spec = {"fps_num": info.fps_n, "fps_den": info.fps_d,
                         "frames": frames, "sha256": digest,
                         "width": info.width, "height": info.height,
                         "decoder": self.decoder, "format": "NV12"}
            pipeline.set_state(Gst.State.PLAYING)
            if not self._buffer.wait_ready(10, min(self.buffer_frames, frames)):
                raise RuntimeError(self.error or "decoder could not prefill the startup buffer")
            return dict(self.spec)
        except Exception:
            self.stop()
            raise

    def _build_decoder(self, media_path):
        if not Gst.ElementFactory.find(self.decoder):
            raise RuntimeError(f"GStreamer decoder {self.decoder} is not installed")
        pipeline = Gst.Pipeline.new("frame-decoder")
        generation = self._generation
        def on_error(error):
            if generation == self._generation and not self._stopping:
                self.error = error
                self._buffer.close()
        encoded = EncodedStream(str(media_path), self._loop_media,
            lambda: generation == self._generation and not self._stopping,
            on_error, self.trace)
        self._encoded = encoded
        source = encoded.source
        decoder = Gst.ElementFactory.make(self.decoder, "decoder")
        capsfilter = Gst.ElementFactory.make("capsfilter", "video-format")
        capsfilter.set_property("caps", Gst.Caps.from_string("video/x-raw,format=NV12"))
        sink = Gst.ElementFactory.make("appsink", "frames")
        sink.set_property("sync", False)
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 1)
        sink.set_property("drop", False)
        elements = [source, decoder]
        # Software decode is explicit and useful for off-Pi integration tests.
        # The hardware path keeps NV12; it does not convert every pixel to RGBA.
        if self.decoder == "avdec_h264":
            elements.append(Gst.ElementFactory.make("videoconvert", "convert"))
        elements.extend([capsfilter, sink])
        if any(element is None for element in elements):
            raise RuntimeError("required GStreamer demux/parser/video elements are missing")
        for element in elements:
            pipeline.add(element)
        for first, second in zip(elements, elements[1:]):
            if not first.link(second):
                raise RuntimeError(f"could not link {first.name} to {second.name}")
        self.pipeline, self.appsink = pipeline, sink
        sink.connect("new-sample", self._on_sample)
        self._bus = pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message", self._on_bus)
        if self._glib_thread is None:
            self._glib_thread = threading.Thread(target=self._glib_loop.run, daemon=True)
            self._glib_thread.start()
        encoded.start()
        return pipeline, sink

    def _shutdown_decoder(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline.get_state(2 * Gst.SECOND)
        if self._encoded is not None:
            self._encoded.close()
            self._encoded = None
        if self._bus is not None:
            self._bus.remove_signal_watch()
        self.pipeline = self.appsink = self._bus = None

    def slave_to_clock(self, host, port):
        # The clock provider uses IPv4; don't accidentally select an advertised
        # link-local IPv6 address with a different socket/scope requirement.
        address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
        self.net_clock = GstNet.NetClientClock.new("frame-clock", address, port, 0)
        if not self.net_clock.wait_for_sync(5 * Gst.SECOND):
            raise RuntimeError(f"could not synchronize media clock at {host}:{port}")

    def clock_time_ns(self):
        if self.net_clock is None or not self.net_clock.is_synced():
            raise RuntimeError("master media clock is not synchronized")
        return self.net_clock.get_time()

    def buffered_range(self):
        return self._buffer.range()

    def take_frame(self, sequence):
        return self._buffer.take(sequence)

    def mark_presented(self, frame):
        self._presented = frame.sequence
        self._media_pos_ns = frame.pts

    def media_pos_ns(self):
        # Report the actual displayed frame, never an extrapolated pipeline query.
        return self._media_pos_ns

    def decoder_ok(self):
        return self._decoder_ok and self.error is None

    def clock_offset_ns(self):
        if self.net_clock is not None:
            return self.net_clock.get_internal_time() - self.net_clock.get_time()
        return 0

    def stop(self):
        self._stopping = True
        self._generation += 1
        self._buffer.close()  # release any producer blocked by decode-ahead
        with self._pipeline_lock:
            self._shutdown_decoder()
            self.net_clock = None
            self.spec = None
            self._decoder_ok = False

    def _on_sample(self, sink):
        callback_start = time.monotonic_ns()
        buffer_queue = self._buffer
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf, caps = sample.get_buffer(), sample.get_caps()
        info = GstVideo.VideoInfo.new_from_caps(caps)
        metadata = GstVideo.buffer_get_video_meta(buf)
        strides = metadata.stride if metadata else info.stride
        offsets = metadata.offset if metadata else info.offset
        ok, mapped = buf.map(Gst.MapFlags.READ)
        if not ok:
            self.error = "could not map decoded video buffer"
            return Gst.FlowReturn.ERROR
        try:
            import numpy as np
            planes = []
            for plane, height in ((0, info.height), (1, (info.height + 1) // 2)):
                # Hardware buffers may have aligned strides/offsets. GLES2 has no
                # UNPACK_ROW_LENGTH, so pack each plane explicitly and correctly.
                array = np.ndarray((height, info.width), dtype=np.uint8,
                                   buffer=mapped.data, offset=offsets[plane],
                                   strides=(strides[plane], 1))
                # Reading decoder memory can take several milliseconds. NumPy's
                # copy releases the GIL during that read; tobytes() directly on
                # the mapped view holds it and stalls the rendering thread.
                # Convert the cached copy to independently owned frame bytes.
                planes.append(array.copy(order="C").tobytes())
            segment = sample.get_segment()
            pts = int(segment.to_stream_time(Gst.Format.TIME, buf.pts))
            if self._pts_origin is None:
                self._pts_origin = pts
            pts -= self._pts_origin
            if self.spec:
                expected = self._sequence * Gst.SECOND * info.fps_d // info.fps_n
                if pts == Gst.CLOCK_TIME_NONE or abs(pts - expected) > Gst.SECOND * info.fps_d // (2 * info.fps_n):
                    raise RuntimeError(f"video timestamps are not a continuous constant-frame-rate sequence: "
                                       f"frame {self._sequence}, pts={pts}, expected={expected}")
            matrix = "bt709" if info.colorimetry.matrix == GstVideo.VideoColorMatrix.BT709 else "bt601"
            # GST_VIDEO_COLOR_RANGE_0_255 = 1; GI's leading-digit Python name
            # differs between typelib/binding versions.
            full_range = int(info.colorimetry.range) == 1
            frame = Frame(tuple(planes), info.width, info.height,
                          pts % (self.spec["frames"] * Gst.SECOND * info.fps_d // info.fps_n) if self.spec else pts, self._sequence,
                          matrix=matrix, full_range=full_range)
        except Exception as exc:
            self.error = str(exc)
            buffer_queue.close()
            return Gst.FlowReturn.ERROR
        finally:
            buf.unmap(mapped)
        copied_at = time.monotonic_ns()
        if self.trace:
            self.trace.record("decoded", sequence=frame.sequence, copy_ms=(copied_at-callback_start)/1e6, buffered=buffer_queue.range())
        if buffer_queue.put(frame):
            self._sequence += 1
            self._decoder_ok = True
        return Gst.FlowReturn.OK

    def _on_bus(self, bus, message):
        if bus != self._bus:
            return
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            self.error = self.error or str(error)
            self._buffer.close()
            self._decoder_ok = False
            print(f"[player] ERROR {error}: {debug}")
        elif message.type == Gst.MessageType.EOS:
            if self._loop_media:
                self.error = "continuous decoder unexpectedly reached end of stream"
                self._buffer.close()
            elif self._on_eos:
                self._on_eos()
