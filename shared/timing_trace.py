"""Opt-in bounded timing trace; disk IO stays off playback threads."""
import json
import gc
import os
import queue
import threading
import time
from collections import deque


class TimingTrace:
    def __init__(self):
        self.path = os.environ.get('IMMERSIVE_TRACE_PATH')
        self.dropped = 0
        self._queue = queue.Queue(maxsize=8192) if self.path else None
        if self.path:
            self._gc_started = {}
            # GC may run inside Queue.put while its non-reentrant lock is held.
            # Its callback must not enter that queue again. C deque append/popleft
            # keep these bounded callback events separate until the writer drains.
            self._gc_events = deque(maxlen=128)
            gc.callbacks.append(self._gc_event)
            threading.Thread(target=self._write, daemon=True).start()

    def _gc_event(self, phase, info):
        generation = info["generation"]
        if phase == "start":
            self._gc_started[generation] = time.monotonic_ns()
        else:
            elapsed = time.monotonic_ns() - self._gc_started.pop(generation, time.monotonic_ns())
            if elapsed > 1_000_000:
                if len(self._gc_events) == self._gc_events.maxlen:
                    self.dropped += 1
                self._gc_events.append(dict(event='gc_pause', local_ns=time.monotonic_ns(),
                                            elapsed_ms=elapsed/1e6, generation=generation))

    def record(self, event, **fields):
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(dict(event=event, local_ns=time.monotonic_ns(), **fields))
        except queue.Full:
            self.dropped += 1

    def _write(self):
        try:
            with open(self.path, 'a', buffering=1) as stream:
                while True:
                    try:
                        event = self._queue.get(timeout=.1)
                    except queue.Empty:
                        event = None
                    if event is not None:
                        stream.write(json.dumps(event) + '\n')
                    while self._gc_events:
                        stream.write(json.dumps(self._gc_events.popleft()) + '\n')
        except OSError as exc:
            print(f'[trace] could not write {self.path}: {exc}', flush=True)
