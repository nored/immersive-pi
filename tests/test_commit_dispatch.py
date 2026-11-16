import asyncio
import json
import queue
import threading
from types import SimpleNamespace

import pytest
from agent import RenderNode


def node():
    n=RenderNode.__new__(RenderNode)
    n._session='session';n._last_commit=-1;n._present_lock=threading.RLock()
    n.player=SimpleNamespace(spec={'frames':30})
    n._staged_frames={0:SimpleNamespace(sequence=0)}
    n.sent=[]
    n._presenter=SimpleNamespace(submit=lambda *args:n.sent.append(args))
    n.trace=SimpleNamespace(record=lambda *args,**kwargs:None)
    n._cmd_q=queue.Queue()
    return n


def test_presentation_acknowledges_the_presenters_conversion_without_resampling():
    n = node()
    n.player = SimpleNamespace(net_clock=object(), mark_presented=lambda frame: None)
    n._out_q = queue.Queue()
    n._frame_count = 0
    frame = SimpleNamespace(sequence=3)
    n._complete_presentation(frame, {'present_at_ns': 1000, 'frame': 3}, 'session', 1100)
    packet = n._out_q.get_nowait()
    assert packet['presented_at_ns'] == 1100
    assert packet['presentation_error_ns'] == 100
    assert n._presented == 3 and n._frame_count == 1


def test_commit_bypasses_the_gpu_command_queue():
    n=node()
    async def messages():
        yield json.dumps({'cmd':'stage_frame','session':'session','sequence':1})
        yield json.dumps({'cmd':'commit_frame','session':'session','sequence':0,
                          'frame':0,'present_at_ns':100})
    asyncio.run(n._ws_recv(messages()))
    # The GPU thread has not consumed its queue, but presentation is scheduled.
    assert n._cmd_q.qsize()==1
    assert n._cmd_q.get()['cmd']=='stage_frame'
    assert n.sent[0][0].sequence==0
    assert n._last_commit==0


def test_stale_session_is_ignored_and_noncontiguous_commits_fail():
    n=node()
    n._receive_commit({'session':'stale'})
    assert not n.sent
    with pytest.raises(RuntimeError,match='non-contiguous'):
        n._receive_commit({'session':'session','sequence':2,'frame':2})
