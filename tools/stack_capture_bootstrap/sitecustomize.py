"""Opt-in startup hook for a temporary renderer diagnostic PYTHONPATH.

Registers a nonfatal native stack-dump signal before any GStreamer/GLES calls.
The PID marker lets the diagnostic runner verify registration before signalling.
This helper is not part of the production runtime or image.
"""
import os

if os.environ.get('IMMERSIVE_STACK_CAPTURE') == '1':
    import faulthandler
    from pathlib import Path
    import signal

    faulthandler.enable(all_threads=True)
    faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    directory = Path(os.environ.get('IMMERSIVE_STACK_DIR', '/run'))
    (directory / f'immersive-stack-ready-{os.getpid()}').write_text('SIGUSR1 registered\n')
