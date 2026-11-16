import threading
from types import SimpleNamespace

import pytest

from shared.frame_sync import DecodeBuffer, FrameCoordinator, VideoSpec


def test_no_frame_is_committed_until_every_member_has_it():
    spec = VideoSpec(30, 1, 1440, "same-file")
    master = FrameCoordinator(["a", "b"], spec)
    master.report("a", 0, 7, -1)
    assert master.commit(0) is None
    master.report("b", 0, 7, -1)
    assert master.commit(0) == {"sequence": 0, "frame": 0, "present_at_ns": 100_000_000}
    assert master.commit(0)["sequence"] == 1
    assert master.commit(0)["sequence"] == 2
    assert master.commit(0) is None  # bounded unacknowledged presentation horizon


def test_slow_member_holds_everyone_without_skipping_and_loops_by_frame_number():
    master = FrameCoordinator(["a", "b"], VideoSpec(30, 1, 2, "same"))
    master.report("a", 0, 7, -1)
    master.report("b", 0, 0, -1)
    assert master.commit(0)["frame"] == 0
    assert master.commit(1_000_000_000) is None
    master.report("a", 1, 7, 0)
    master.report("b", 1, 7, 0)
    resumed = master.commit(1_000_000_000)
    assert resumed["sequence"] == 1 and resumed["frame"] == 1
    assert resumed["present_at_ns"] == 1_100_000_000
    assert master.holds == 1
    assert master.commit(1_000_000_000)["frame"] == 0


def test_fractional_rate_has_no_accumulated_rounding_drift():
    spec = VideoSpec(30000, 1001, 100, "same")
    assert spec.offset_ns(30000) == 1_001_000_000_000
    master = FrameCoordinator(["a"], spec)
    for sequence in range(10000):
        master.report("a", sequence, sequence + 7, sequence - 1)
        commit = master.commit(spec.offset_ns(sequence))
        assert commit["present_at_ns"] == 100_000_000 + spec.offset_ns(sequence)
    assert master.holds == 0


def test_stale_and_invalid_acknowledgements_are_detected():
    master = FrameCoordinator(["a"], VideoSpec(30, 1, 60, "same"))
    master.report("a", 0, 7, -1, now=0)
    assert master.missing(now=3) == ["a"]
    with pytest.raises(ValueError):
        master.report("a", 1, 7, 0)


def test_decode_buffer_applies_backpressure_and_stop_releases_producer():
    buffer = DecodeBuffer(2)
    for sequence in range(2):
        assert buffer.put(SimpleNamespace(sequence=sequence))
    started = threading.Event()
    result = []
    def produce():
        started.set()
        result.append(buffer.put(SimpleNamespace(sequence=2)))
    worker = threading.Thread(target=produce)
    worker.start()
    assert started.wait(1)
    assert buffer.range() == (0, 1)
    assert buffer.take(0).sequence == 0
    worker.join(1)
    assert result == [True] and buffer.range() == (1, 2)
    with pytest.raises(RuntimeError):
        buffer.take(2)
    buffer.close()
    assert not buffer.put(SimpleNamespace(sequence=3))


def test_gpu_readiness_is_required_after_decode_readiness():
    master = FrameCoordinator(["a", "b"], VideoSpec(30, 1, 60, "same"), max_ahead=2, require_staged=True)
    for node in ["a", "b"]:
        master.report(node, 0, 7, -1)
    assert master.commit(0) is None
    assert master.reserve() == {"sequence": 0, "frame": 0}
    master.staged_frame("a", 0)
    assert master.commit(0) is None
    master.staged_frame("b", 0)
    assert master.commit(0) is None  # warm the complete native-buffer horizon
    assert master.reserve()["sequence"] == 1
    master.staged_frame("a", 1)
    master.staged_frame("b", 1)
    assert master.commit(0)["sequence"] == 0


def test_three_native_frames_cover_confirmation_latency_without_holding():
    master = FrameCoordinator(["a"], VideoSpec(30, 1, 60, "same"),
                              max_ahead=3, require_staged=True)
    master.report("a", 0, 23, -1)
    for sequence in range(3):
        assert master.reserve()["sequence"] == sequence
        master.staged_frame("a", sequence)
    for sequence in range(3):
        assert master.commit(0)["sequence"] == sequence
    master.report("a", 3, 26, 0)
    assert master.reserve()["sequence"] == 3
    master.staged_frame("a", 3)
    # Completion reaches the master 50ms after the first display deadline.
    # The next frame still has 50ms of lead, rather than forcing a fleet hold.
    assert master.commit(150_000_000)["present_at_ns"] == 200_000_000
    assert master.holds == 0
