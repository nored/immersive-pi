import pytest

from gl_pipeline import KmsDisplay


def test_staged_native_buffers_have_bounded_ownership_and_cancellation():
    class Backend:
        def __init__(self):
            self.created = 0
            self.released = []
            self.queued = []
        def stage_buffer(self):
            self.created += 1
            return self.created, self.created + 100
        def queue_buffer(self, token):
            self.queued.append(token)
        def release_buffer(self, token):
            self.released.append(token)
    display = KmsDisplay()
    display._backend = Backend()
    display.stage_frame(0)
    display.stage_frame(1)
    display.stage_frame(2)
    with pytest.raises(RuntimeError):
        display.stage_frame(3)
    display.queue_staged_frame(0)
    display.stage_frame(3)
    display.discard_staged_frames()
    assert display._backend.queued == [(1, 101)]
    assert display._backend.released == [(2, 102), (3, 103), (4, 104)]
    assert display._staged_scanout == {}
