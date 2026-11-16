import json
import os
from pathlib import Path
import subprocess
import sys


def test_gc_callback_can_run_while_trace_queue_lock_is_held(tmp_path):
    path = tmp_path / 'trace.jsonl'
    code = '''
import gc,json,time
from pathlib import Path
from shared.timing_trace import TimingTrace
trace=TimingTrace()
def slow_gc(phase, info):
    if phase == 'start': time.sleep(.003)
gc.callbacks.append(slow_gc)
original_put=trace._queue._put
def collect_inside_put(item):
    original_put(item)
    gc.collect()
trace._queue._put=collect_inside_put
# Queue.put holds its own non-reentrant mutex during _put. The old GC callback
# tried another put_nowait here, deadlocking every subsequent tracing thread.
trace.record('inside-queue-lock')
deadline=time.monotonic()+3
while time.monotonic()<deadline:
    path=Path(trace.path)
    rows=[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    if any(row['event']=='gc_pause' for row in rows) and any(row['event']=='inside-queue-lock' for row in rows):
        print('completed')
        break
    time.sleep(.01)
else: raise AssertionError('GC and normal trace events were not drained')
'''
    environment = {**os.environ, 'IMMERSIVE_TRACE_PATH': str(path)}
    child = subprocess.run([sys.executable, '-c', code],
                           cwd=Path(__file__).resolve().parents[1], env=environment,
                           capture_output=True, text=True, timeout=5)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == 'completed'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert any(row['event'] == 'gc_pause' and row['elapsed_ms'] >= 3 for row in rows)


def test_gc_callback_buffer_stays_bounded_without_touching_trace_queue():
    from collections import deque
    import time
    from shared.timing_trace import TimingTrace
    trace = TimingTrace.__new__(TimingTrace)
    trace._gc_started = {}
    trace._gc_events = deque(maxlen=128)
    trace.dropped = 0
    # No trace queue exists here: a GC callback must never attempt to use one.
    for _ in range(200):
        trace._gc_started[0] = time.monotonic_ns() - 2_000_000
        trace._gc_event('stop', {'generation': 0})
    assert len(trace._gc_events) == 128 and trace.dropped == 72
