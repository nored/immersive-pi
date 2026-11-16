"""Protocol v2: bounded decode-ahead and numbered, clocked frame commits.

Video files remain local. The master commits a sequence only after every member
has that decoded frame. Presentation timestamps use the shared GstNet clock.
Readiness delays hold the last frame on the whole fleet; they never seek or skip
content to catch up. Physical HDMI scanout phase still depends on the hardware.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass

PROTOCOL_VERSION = 2


@dataclass(frozen=True)
class VideoSpec:
    fps_num: int
    fps_den: int
    frames: int
    sha256: str

    def __post_init__(self):
        if min(self.fps_num, self.fps_den, self.frames) <= 0:
            raise ValueError("invalid video frame rate or length")

    def offset_ns(self, sequence):
        # Rational arithmetic avoids accumulated rounding errors at 30000/1001.
        return sequence * 1_000_000_000 * self.fps_den // self.fps_num

    def frame_index(self, sequence):
        return sequence % self.frames


class DecodeBuffer:
    """Bounded producer/consumer queue. Each decoded frame is consumed once."""

    def __init__(self, capacity=8):
        if capacity < 2:
            raise ValueError("decode buffer must hold at least two frames")
        self.capacity = capacity
        self._frames = deque()
        self._closed = False
        self._condition = threading.Condition()

    def put(self, frame):
        with self._condition:
            self._condition.wait_for(lambda: self._closed or len(self._frames) < self.capacity)
            if self._closed:
                return False
            self._frames.append(frame)
            self._condition.notify_all()
            return True

    def take(self, sequence):
        with self._condition:
            if not self._frames:
                return None
            frame = self._frames[0]
            if frame.sequence != sequence:
                raise RuntimeError(f"expected decoded frame {sequence}, found {frame.sequence}")
            self._frames.popleft()
            self._condition.notify_all()
            return frame

    def range(self):
        with self._condition:
            if not self._frames:
                return None, None
            return self._frames[0].sequence, self._frames[-1].sequence

    def wait_ready(self, timeout, minimum=1):
        if not 1 <= minimum <= self.capacity:
            raise ValueError("readiness threshold exceeds decode buffer capacity")
        with self._condition:
            self._condition.wait_for(lambda: self._closed or len(self._frames) >= minimum, timeout)
            return len(self._frames) >= minimum

    def close(self):
        with self._condition:
            self._closed = True
            self._frames.clear()
            self._condition.notify_all()


class FrameCoordinator:
    """All-member readiness barrier with a bounded presentation horizon."""

    def __init__(self, members, spec, lead_ns=100_000_000, max_ahead=3, require_staged=False):
        if not members:
            raise ValueError("a frame session needs at least one member")
        self.members = frozenset(members)
        self.spec = spec
        self.lead_ns = lead_ns
        self.max_ahead = max_ahead
        self.ready = {}
        self.presented = {member: -1 for member in self.members}
        self.last_seen = {}
        self.sequence = 0
        self.last_at_ns = None
        self.holds = 0
        self.require_staged = require_staged
        self.staged = {member: -1 for member in self.members}
        self.stage_sequence = 0

    def reserve(self):
        """Reserve GPU staging slots only when every decoder has the frame."""
        sequence = self.stage_sequence
        if sequence - min(self.presented.values()) > self.max_ahead:
            return None
        for member in self.members:
            first, last = self.ready.get(member, (None, None))
            if first is None or not first <= sequence <= last:
                return None
        self.stage_sequence += 1
        return {"sequence": sequence, "frame": self.spec.frame_index(sequence)}

    def staged_frame(self, member, sequence):
        if member not in self.members or sequence != self.staged[member] + 1 or sequence >= self.stage_sequence:
            raise ValueError(f"invalid GPU staging acknowledgement from {member}")
        self.staged[member] = sequence

    def report(self, member, first, last, presented, now=None):
        if member not in self.members:
            return
        if presented < self.presented[member] or presented >= self.sequence:
            raise ValueError(f"invalid presentation acknowledgement from {member}")
        self.ready[member] = (first, last)
        self.presented[member] = presented
        self.last_seen[member] = time.monotonic() if now is None else now

    def missing(self, now=None, timeout=2):
        now = time.monotonic() if now is None else now
        return sorted(m for m in self.members if now - self.last_seen.get(m, float('-inf')) > timeout)

    def commit(self, now_ns):
        sequence = self.sequence
        if sequence - min(self.presented.values()) > self.max_ahead:
            return None
        if self.require_staged:
            required = self.max_ahead - 1 if sequence == 0 else sequence
            if any(self.staged[m] < required for m in self.members):
                return None
        else:
            for member in self.members:
                first, last = self.ready.get(member, (None, None))
                if first is None or not first <= sequence <= last:
                    return None
        if self.last_at_ns is None:
            at = now_ns + self.lead_ns
        else:
            delta = self.spec.offset_ns(sequence) - self.spec.offset_ns(sequence - 1)
            at = self.last_at_ns + delta
            if at < now_ns + min(10_000_000, self.lead_ns // 4):
                at = now_ns + self.lead_ns
                self.holds += 1
        self.sequence += 1
        self.last_at_ns = at
        return {"sequence": sequence, "frame": self.spec.frame_index(sequence),
                "present_at_ns": at}
