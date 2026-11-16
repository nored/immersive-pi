"""Own DRM flip timing independently of the GLES staging thread.

This worker never calls EGL/GLES. Buffers are already rendered and locked before
submission. Stop joins the worker and drains an in-flight flip before the caller
may resume static drawing or release native buffers.
"""
import queue
import threading
import time


class ClockMapping:
    """Bracket clock reads so thread scheduling cannot bias the offset silently."""
    def __init__(self, clock_ns, local_ns=time.monotonic_ns):
        self.clock_ns, self.local_ns = clock_ns, local_ns
        self.offset_ns = None
        self.sampled_at_ns = None
        self.uncertainty_ns = None
        self.sample_span_ns = None
        self.reused = False

    def convert(self, timestamp):
        best = None
        for _ in range(3):
            before = self.local_ns()
            remote = self.clock_ns()
            after = self.local_ns()
            sample = (after - before, remote - (before + after) // 2, after)
            if best is None or sample[0] < best[0]:
                best = sample
            if sample[0] <= 200_000:
                break
        span, offset, sampled_at = best
        self.sample_span_ns = span
        # A recent good sample is safer than a read interrupted for milliseconds.
        self.reused = (span > 200_000 and self.offset_ns is not None and
                       self.uncertainty_ns * 2 <= span and
                       sampled_at - self.sampled_at_ns < 1_000_000_000)
        if not self.reused:
            self.offset_ns, self.sampled_at_ns = offset, sampled_at
            self.uncertainty_ns = (span + 1) // 2
        return timestamp + self.offset_ns


def submission_time_ns(deadline, last_flip, period, previous_deadline=None):
    """Queue after the preceding refresh, ahead of the nearest desired refresh.

    DRM presents a queued buffer on the next refresh. Waiting until the video
    deadline leaves almost no submission margin when it falls near a refresh.
    Once phase is known, choose the nearest refresh and use its preceding
    interval, leaving two milliseconds for the preceding refresh to pass.
    """
    if last_flip is None or not period or period <= 4_000_000:
        return deadline - 1_000_000
    steps = max(1, (deadline - last_flip + period // 2) // period)
    if previous_deadline is not None and deadline > previous_deadline:
        cadence_steps = max(1, (deadline - previous_deadline + period // 2) // period)
        cadence_target = last_flip + cadence_steps * period
        # A clock-offset sample can move slightly across the half-refresh
        # boundary. Preserve cadence there instead of alternating 3/1 refreshes.
        # The bound still permits correction when source/display rates drift.
        if abs(cadence_target - deadline) <= period // 2 + 1_500_000:
            steps = cadence_steps
    return last_flip + (steps - 1) * period + 2_000_000


class Presenter:
    def __init__(self, display, clock_ns, completed, failed, trace=None):
        self.display, self.clock_ns = display, clock_ns
        self.completed, self.failed = completed, failed
        self.trace = trace
        self.clock_mapping = ClockMapping(clock_ns)
        self._queue = queue.Queue(maxsize=3)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, frame, commit, session):
        if self._stop.is_set():
            raise RuntimeError('presentation worker is stopped')
        self._queue.put_nowait((frame, commit, session))

    def stop(self):
        self._stop.set()
        self._thread.join(1)
        if self._thread.is_alive():
            raise RuntimeError('presentation worker did not stop safely')

    def _complete(self, pending, timestamp):
        master_timestamp = self.clock_mapping.convert(timestamp)
        if self.trace:
            self.trace('flip_clock', session=pending[2], sequence=pending[0].sequence,
                       flip_local_ns=timestamp, flip_master_ns=master_timestamp,
                       sample_span_ns=self.clock_mapping.sample_span_ns,
                       uncertainty_ns=self.clock_mapping.uncertainty_ns,
                       reused=self.clock_mapping.reused)
        # The callback and the next scheduling decision share this exact mapping.
        self.completed(*pending, master_timestamp)
        return master_timestamp

    def _initial_phase(self, period):
        query = getattr(self.display, 'current_refresh_ns', None)
        if not query or not period:
            return None
        timestamp = query()
        if timestamp is None:
            return None
        # A vblank timestamp may lie just ahead of now while inside vblank.
        age = time.monotonic_ns() - timestamp
        if not -period <= age <= 2 * period:
            return None
        master_timestamp = self.clock_mapping.convert(timestamp)
        if self.trace:
            self.trace('phase_seed', flip_local_ns=timestamp, flip_master_ns=master_timestamp,
                       age_ns=age, period_ns=period)
        return master_timestamp

    def _run(self):
        pending = waiting = None
        last_flip = None
        previous_deadline = None
        phase_probed = False
        period = getattr(self.display, 'refresh_period_ns', None)
        try:
            while not self._stop.is_set() or pending is not None:
                if pending is not None:
                    timestamp = self.display.poll_flip()
                    if timestamp is not None:
                        last_flip = self._complete(pending, timestamp)
                        pending = None
                if self._stop.is_set():
                    time.sleep(.001)
                    continue
                if waiting is None:
                    try:
                        waiting = self._queue.get_nowait()
                    except queue.Empty:
                        pass
                if pending is None and waiting is not None:
                    frame, commit, session = waiting
                    if not phase_probed:
                        last_flip = self._initial_phase(period)
                        phase_probed = True
                    queue_at = submission_time_ns(commit['present_at_ns'], last_flip, period, previous_deadline)
                    now = self.clock_ns()
                    if now >= queue_at:
                        pending, waiting = waiting, None
                        timestamp = self.display.queue_staged_frame(frame.sequence)
                        previous_deadline = commit['present_at_ns']
                        if self.trace:
                            self.trace('flip_submitted', session=session, sequence=frame.sequence,
                                       deadline_ns=commit['present_at_ns'], queue_at_ns=queue_at,
                                       submitted_master_ns=now, returned_master_ns=self.clock_ns(),
                                       last_flip_ns=last_flip,
                                       period_ns=period)
                        if timestamp is not None:
                            last_flip = self._complete(pending, timestamp)
                            pending = None
                time.sleep(.0005)
        except Exception as exc:
            self._stop.set()
            self.failed(str(exc))
