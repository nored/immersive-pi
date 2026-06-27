"""clock_master.py — the master media clock for the whole room.

A GstNetTimeProvider publishes the control node's GstSystemClock on a fixed UDP
port. Every render node slaves a GstNet.NetClientClock to it, so all nodes share
one media timebase and decode the same frame at the same wall time.

This clock is what keeps media *position* aligned. It does NOT and cannot align
HDMI scanout phase — that runs off each Pi's own pixel crystal below the network.
That is the seam risk the build order surfaces; it is not solvable here.

Run standalone:  python3 clock_master.py --port 8555
"""

from __future__ import annotations

import argparse
import signal
import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstNet", "1.0")
from gi.repository import Gst, GstNet, GLib  # noqa: E402


class ClockMaster:
    def __init__(self, port: int = 8555):
        Gst.init(None)
        self.port = port
        self.clock = Gst.SystemClock.obtain()
        # address 0.0.0.0 so nodes on the dedicated switch can reach it
        self.provider = GstNet.NetTimeProvider.new(self.clock, "0.0.0.0", port)
        self._loop = GLib.MainLoop()

    def base_time_ns(self) -> int:
        """A 'now' on the master clock, in ns — the reference the controller
        adds its lead time to when it issues play_at."""
        return self.clock.get_time()

    def run(self):
        print(f"[clock] GstNetTimeProvider serving SystemClock on udp/{self.port}")
        try:
            self._loop.run()
        except KeyboardInterrupt:
            pass

    def stop(self):
        self._loop.quit()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8555)
    args = ap.parse_args(argv)
    master = ClockMaster(args.port)
    signal.signal(signal.SIGINT, lambda *_: master.stop())
    signal.signal(signal.SIGTERM, lambda *_: master.stop())
    master.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
