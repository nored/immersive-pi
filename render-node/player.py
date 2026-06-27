"""player.py — GStreamer hardware decode, slaved to the control node's net clock.

Pipeline:
    filesrc ! qtdemux ! h264parse ! v4l2h264dec ! videoconvert
    ! video/x-raw,format=RGBA ! appsink

The pipeline clock is replaced by a GstNet.NetClientClock pointed at the
control node, and the base time is set from the controller's `play_at` so every
node maps the same running-time to the same media frame. Decoded RGBA frames
are handed to the GL stage as the latest-frame slot; the GL loop samples it.

Note: RGBA via videoconvert is the simplest correct path. The documented
optimisation (see DEPLOY.md) is to keep NV12 and convert in the GL shader to
take CPU off the Pi 4 at 1080p60.
"""

from __future__ import annotations

import threading
from typing import Optional

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GstNet", "1.0")
from gi.repository import Gst, GstNet, GLib  # noqa: E402

Gst.init(None)


class Frame:
    __slots__ = ("data", "width", "height", "pts")

    def __init__(self, data, width, height, pts):
        self.data = data          # bytes, RGBA tightly packed
        self.width = width
        self.height = height
        self.pts = pts            # buffer PTS in ns (media position)


class Player:
    def __init__(self, on_eos=None):
        self.pipeline: Optional[Gst.Pipeline] = None
        self.appsink: Optional[Gst.Element] = None
        self.net_clock = None
        self._latest: Optional[Frame] = None
        self._lock = threading.Lock()
        self._new = threading.Event()
        self._media_pos_ns = 0
        self._decoder_ok = False
        self._on_eos = on_eos
        self._loop_media = True
        self._loop_epoch_ns: Optional[int] = None   # shared canonical loop epoch
        self._period_ns: Optional[int] = None        # this clip's duration
        self._glib_loop = GLib.MainLoop()
        self._glib_thread: Optional[threading.Thread] = None

    # ---- build -----------------------------------------------------------
    def prepare(self, media_path: str, loop: bool = True):
        """Build the pipeline and bring it to PAUSED (preroll). Idempotent:
        tears down any previous pipeline first."""
        self.stop()
        self._loop_media = loop
        desc = (
            f'filesrc location="{media_path}" ! qtdemux ! h264parse '
            f'! v4l2h264dec ! videoconvert ! video/x-raw,format=RGBA '
            f'! appsink name=sink emit-signals=true sync=true '
            f'max-buffers=2 drop=false'
        )
        self.pipeline = Gst.parse_launch(desc)
        self.appsink = self.pipeline.get_by_name("sink")
        self.appsink.connect("new-sample", self._on_sample)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

        if self._glib_thread is None:
            self._glib_thread = threading.Thread(target=self._glib_loop.run, daemon=True)
            self._glib_thread.start()

        self.pipeline.set_state(Gst.State.PAUSED)
        self.pipeline.get_state(Gst.CLOCK_TIME_NONE)  # block on preroll
        self._decoder_ok = True

    def slave_to_clock(self, host: str, port: int):
        """Attach a NetClientClock so this node shares the master timebase."""
        self.net_clock = GstNet.NetClientClock.new("netclock", host, port, 0)
        # wait until the client clock has synced to the master
        self.net_clock.wait_for_sync(Gst.SECOND)
        if self.pipeline:
            self.pipeline.use_clock(self.net_clock)

    def play_at(self, base_time_ns: int, clock_host: str, clock_port: int):
        """Arm and fire: slave the clock, set the shared base time, go PLAYING.
        running_time = clock - base_time, identical on every node, so all nodes
        present the same media frame."""
        if self.pipeline is None:
            raise RuntimeError("prepare() must be called before play_at()")
        if self.net_clock is None:
            self.slave_to_clock(clock_host, clock_port)
        self.pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
        self.pipeline.set_base_time(base_time_ns)
        self.pipeline.set_state(Gst.State.PLAYING)

    def seek_to(self, position_ns: int):
        """Used by the synced loop boundary to put every node on the canonical
        loop epoch."""
        if self.pipeline:
            self.pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                max(0, position_ns),
            )

    # ---- synced loop boundary (Milestone 3) ------------------------------
    def set_loop_epoch(self, epoch_ns: int):
        """All nodes share one epoch on the net clock; each derives the same
        target media position from it, so 12 loops never fan apart. The period
        is this clip's own duration, identical on every node (same file)."""
        self._loop_epoch_ns = epoch_ns
        self._period_ns = self._duration_ns()

    def _duration_ns(self) -> Optional[int]:
        if self.pipeline:
            ok, dur = self.pipeline.query_duration(Gst.Format.TIME)
            if ok and dur > 0:
                return dur
        return self._period_ns

    def expected_pos_ns(self) -> Optional[int]:
        """Where the shared clock says this node should be in the clip now."""
        if self._loop_epoch_ns is None or self.net_clock is None:
            return None
        period = self._period_ns or self._duration_ns()
        if not period:
            return None
        self._period_ns = period
        return (self.net_clock.get_time() - self._loop_epoch_ns) % period

    def resync_if_needed(self, tolerance_ns: int = 8_000_000):
        """Periodic guard against long-run drift: if this node's media position
        has wandered from the shared expectation by more than ~half a frame at
        24 fps, seek it back onto the epoch. Cheap when already aligned."""
        exp = self.expected_pos_ns()
        if exp is None:
            return
        cur = self.media_pos_ns()
        period = self._period_ns
        diff = abs(cur - exp)
        if period:
            diff = min(diff, period - diff)   # account for wrap at the loop seam
        if diff > tolerance_ns:
            self.seek_to(exp)

    def stop(self):
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.appsink = None
            self._decoder_ok = False

    # ---- frame delivery --------------------------------------------------
    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        w = caps.get_value("width")
        h = caps.get_value("height")
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            frame = Frame(bytes(mapinfo.data), w, h, buf.pts)
        finally:
            buf.unmap(mapinfo)
        with self._lock:
            self._latest = frame
            self._media_pos_ns = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else self._media_pos_ns
        self._new.set()
        return Gst.FlowReturn.OK

    def get_frame(self, timeout: float = 0.0) -> Optional[Frame]:
        """Return the most recent decoded frame (RGBA). Non-blocking by default;
        the GL loop calls this each iteration and re-uploads when changed."""
        if timeout:
            self._new.wait(timeout)
        self._new.clear()
        with self._lock:
            return self._latest

    # ---- status for heartbeat -------------------------------------------
    def media_pos_ns(self) -> int:
        if self.pipeline:
            ok, pos = self.pipeline.query_position(Gst.Format.TIME)
            if ok:
                return pos
        with self._lock:
            return self._media_pos_ns

    def decoder_ok(self) -> bool:
        return self._decoder_ok

    def clock_offset_ns(self) -> int:
        """Net clock's internal-vs-master offset — what the heartbeat reports."""
        if self.net_clock is not None:
            try:
                return int(self.net_clock.get_property("internal-clock").get_time()
                           - self.net_clock.get_time())
            except Exception:
                return 0
        return 0

    # ---- bus -------------------------------------------------------------
    def _on_bus(self, _bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            if self._loop_media and self.pipeline:
                # keep looping with no controller round-trip. If a shared loop
                # epoch is set, seek onto it so every node restarts on the same
                # frame; otherwise just restart from the top.
                exp = self.expected_pos_ns()
                self.seek_to(exp if exp is not None else 0)
            elif self._on_eos:
                self._on_eos()
        elif t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f"[player] ERROR {err}: {dbg}")
            self._decoder_ok = False
