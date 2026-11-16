import os
from pathlib import Path
import select
import signal
import subprocess
import sys


def test_stack_signal_dumps_without_killing_the_diagnostic_process(tmp_path):
    bootstrap = Path(__file__).resolve().parents[1] / 'tools/stack_capture_bootstrap'
    environment = {**os.environ, 'PYTHONPATH': str(bootstrap),
                   'IMMERSIVE_STACK_CAPTURE': '1', 'IMMERSIVE_STACK_DIR': str(tmp_path)}
    child = subprocess.Popen([sys.executable, '-c',
        'print("ready",flush=True);input();print("survived",flush=True)'],
        env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True)
    try:
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline().strip() == 'ready'
        assert (tmp_path / f'immersive-stack-ready-{child.pid}').read_text() == 'SIGUSR1 registered\n'
        os.kill(child.pid, signal.SIGUSR1)
        assert select.select([child.stderr], [], [], 5)[0]
        first = child.stderr.readline()
        stdout, stderr = child.communicate('\n', timeout=5)
        assert child.returncode == 0 and 'survived' in stdout
        assert 'most recent call first' in first + stderr
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
