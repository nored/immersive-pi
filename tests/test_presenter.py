import threading
import time
from types import SimpleNamespace

from presenter import ClockMapping, Presenter, submission_time_ns


class SampleClock:
    def __init__(self, durations, offset=123_000):
        self.now = 0
        self.durations = iter(durations)
        self.offset = offset

    def local(self):
        return self.now

    def remote(self):
        duration = next(self.durations)
        value = self.now + duration // 2 + self.offset
        self.now += duration
        return value


def test_clock_mapping_retries_a_preempted_read():
    clock = SampleClock([10_000_000, 100])
    mapping = ClockMapping(clock.remote, clock.local)
    assert mapping.convert(1_000_000) == 1_123_000
    assert mapping.sample_span_ns == 100 and mapping.uncertainty_ns == 50
    assert not mapping.reused


def test_clock_mapping_keeps_recent_offset_when_all_reads_are_slow():
    clock = SampleClock([100, 3_000_000, 2_000_000, 4_000_000, 100])
    mapping = ClockMapping(clock.remote, clock.local)
    assert mapping.convert(0) == 123_000
    clock.offset = 999_000
    assert mapping.convert(0) == 123_000
    assert mapping.reused and mapping.sample_span_ns == 2_000_000
    assert mapping.convert(0) == 999_000
    assert not mapping.reused


def test_clock_mapping_refreshes_stale_offset_and_records_uncertainty():
    clock = SampleClock([100, 3_000_000, 2_000_000, 4_000_000])
    mapping = ClockMapping(clock.remote, clock.local)
    mapping.convert(0)
    clock.now += 2_000_000_000
    clock.offset = 999_000
    assert mapping.convert(0) == 999_000
    assert not mapping.reused and mapping.uncertainty_ns == 1_000_000


def test_clock_mapping_improves_an_initial_slow_sample():
    clock = SampleClock([3_000_000] * 3 + [1_000_000] * 3)
    mapping = ClockMapping(clock.remote, clock.local)
    mapping.convert(0)
    assert mapping.uncertainty_ns == 1_500_000
    clock.offset = 999_000
    assert mapping.convert(0) == 999_000
    assert not mapping.reused and mapping.uncertainty_ns == 500_000


def test_initial_phase_uses_fresh_display_timestamp_and_rejects_stale_phase():
    period = 16_720_000
    phase = time.monotonic_ns() - 5_000_000
    worker = Presenter.__new__(Presenter)
    worker.display = SimpleNamespace(current_refresh_ns=lambda: phase)
    worker.clock_mapping = SimpleNamespace(convert=lambda timestamp: timestamp + 123_000)
    events = []
    worker.trace = lambda event, **fields: events.append((event, fields))
    assert worker._initial_phase(period) == phase + 123_000
    assert events[0][0] == 'phase_seed'
    phase -= 1_000_000_000
    assert worker._initial_phase(period) is None


def test_refresh_phase_is_queried_when_first_commit_arrives():
    calls, failures = [], []
    done = threading.Event()
    class Display:
        refresh_period_ns = 16_720_000
        def current_refresh_ns(self):
            calls.append('phase')
            return time.monotonic_ns() - 1_000_000
        def queue_staged_frame(self, sequence):
            calls.append('queue')
            return time.monotonic_ns()
    worker = Presenter(Display(), time.monotonic_ns, lambda *args: done.set(), failures.append)
    try:
        assert not calls
        worker.submit(SimpleNamespace(sequence=0), {'present_at_ns': time.monotonic_ns()+30_000_000}, 'test')
        assert done.wait(1)
        assert calls == ['phase', 'queue'] and not failures
    finally:
        worker.stop()


def test_submission_window_targets_nearest_refresh_with_margin():
    period = 16_720_000
    anchor = 1_000_000_000
    # Both sides of a refresh get the same wide submission window.
    for offset in (-1_000_000, 0, 1_000_000):
        assert submission_time_ns(anchor + 2 * period + offset, anchor, period) == anchor + period + 2_000_000
    # Crossing the midpoint selects the following refresh, never an earlier one.
    assert submission_time_ns(anchor + 2 * period + period // 2, anchor, period) == anchor + 2 * period + 2_000_000
    assert submission_time_ns(anchor - period, anchor, period) == anchor + 2_000_000


def test_unmeasured_phase_keeps_initial_deadline_behavior():
    assert submission_time_ns(100_000_000, None, 16_720_000) == 99_000_000
    assert submission_time_ns(100_000_000, 0, None) == 99_000_000


def test_optional_trace_reports_the_actual_submission_decision():
    events, failures = [], []
    done = threading.Event()
    class Display:
        refresh_period_ns = 16_720_000
        def queue_staged_frame(self, sequence):
            return time.monotonic_ns()
    worker = Presenter(Display(), time.monotonic_ns, lambda *args: done.set(),
                       failures.append, trace=lambda event, **fields: events.append((event, fields)))
    try:
        deadline = time.monotonic_ns() + 10_000_000
        worker.submit(SimpleNamespace(sequence=0), {'present_at_ns': deadline}, 'test')
        assert done.wait(1)
        assert not failures
        event, fields = events[0]
        assert event == 'flip_submitted' and fields['session'] == 'test'
        assert fields['queue_at_ns'] == deadline - 1_000_000
        assert fields['submitted_master_ns'] >= fields['queue_at_ns']
        assert fields['last_flip_ns'] is None
    finally:
        worker.stop()


def test_matched_cadence_survives_submission_jitter_near_refresh():
    period = 16_720_000
    last = 1_000_000_000
    for frame in range(1, 101):
        deadline = 1_000_000_000 + frame * 2 * period + 100_000
        queued = submission_time_ns(deadline, last, period) + (frame % 4) * 1_000_000
        # A next-vblank DRM model: even 3ms of scheduling jitter is harmless.
        actual = last + ((queued - last) // period + 1) * period
        assert actual - last == 2 * period
        last = actual


def test_clock_offset_noise_at_half_refresh_does_not_alternate_cadence():
    period = 16_720_000
    true_flip = 1_000_000_000
    previous_deadline = true_flip + period // 2
    for frame in range(100):
        deadline = previous_deadline + 2 * period
        estimated_flip = true_flip + (-800_000 if frame % 2 else 800_000)
        queued = submission_time_ns(deadline, estimated_flip, period, previous_deadline)
        actual = true_flip + ((queued - true_flip) // period + 1) * period
        assert actual - true_flip == 2 * period
        true_flip, previous_deadline = actual, deadline


def test_cadence_hysteresis_still_corrects_real_source_display_rate_drift():
    period = 16_719_521
    last_flip = previous_deadline = 1_000_000_000
    short_frames = 0
    for frame in range(1, 1001):
        deadline = 1_000_000_000 + round(frame * 1e9 / 30)
        queued = submission_time_ns(deadline, last_flip, period, previous_deadline)
        actual = last_flip + ((queued - last_flip) // period + 1) * period
        steps = (actual - last_flip) // period
        assert steps in (1, 2)
        short_frames += steps == 1
        assert abs(actual - deadline) <= period // 2 + 1_500_000
        last_flip, previous_deadline = actual, deadline
    assert 5 <= short_frames <= 7


def test_display_timing_runs_while_gpu_producer_is_blocked():
    completed, failed = [], []
    event = threading.Event()
    class Display:
        pending = None
        def queue_staged_frame(self, sequence):
            self.pending = sequence
        def poll_flip(self):
            if self.pending is None:
                return None
            self.pending = None
            return time.monotonic_ns()
    def presented(frame, commit, session, timestamp):
        completed.append((frame.sequence, threading.get_ident(), timestamp))
        if len(completed) == 2:
            event.set()
    worker = Presenter(Display(), time.monotonic_ns, presented, failed.append)
    try:
        start = time.monotonic_ns()
        for sequence in range(2):
            worker.submit(SimpleNamespace(sequence=sequence),
                          {'present_at_ns': start+(sequence+1)*20_000_000}, 'session')
        # Simulates a GPU wait on the producer: it cannot submit any DRM work.
        assert event.wait(1)
        assert [x[0] for x in completed] == [0, 1]
        assert all(x[1] != threading.get_ident() for x in completed)
        assert not failed
    finally:
        worker.stop()


def test_stop_drains_submitted_flip_and_cancels_future_commits():
    submitted, release = threading.Event(), threading.Event()
    completed, failed = [], []
    class Display:
        def queue_staged_frame(self, sequence):
            submitted.set()
        def poll_flip(self):
            return time.monotonic_ns() if release.is_set() else None
    worker = Presenter(Display(), time.monotonic_ns,
                       lambda *args: completed.append(args[0].sequence), failed.append)
    worker.submit(SimpleNamespace(sequence=0), {'present_at_ns': 0}, 'session')
    worker.submit(SimpleNamespace(sequence=1), {'present_at_ns': 10**30}, 'session')
    assert submitted.wait(1)
    release.set()
    worker.stop()
    assert completed == [0]
    assert not failed
