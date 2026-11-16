import errno
import struct

import pytest
import drm_kms


@pytest.mark.parametrize('active,timestamp,expected', [(1,1234,1234),(0,1234,None),(1,0,None)])
def test_read_only_phase_query_checks_active_output(monkeypatch, active, timestamp, expected):
    def ioctl(fd, command, request, mutate):
        assert fd == 7 and command == 0xC018643B and mutate
        assert struct.unpack('=IIQq',request) == (100,0,0,0)
        request[:] = struct.pack('=IIQq',100,active,42,timestamp)
    monkeypatch.setattr(drm_kms.fcntl,'ioctl',ioctl)
    assert drm_kms.current_refresh_ns(7,100) == expected


def test_unsupported_query_falls_back_but_io_failure_is_reported(monkeypatch):
    def fail(code):
        def ioctl(*args):
            raise OSError(code,'test')
        monkeypatch.setattr(drm_kms.fcntl,'ioctl',ioctl)
    fail(errno.EOPNOTSUPP)
    assert drm_kms.current_refresh_ns(7,100) is None
    fail(errno.EIO)
    with pytest.raises(OSError):
        drm_kms.current_refresh_ns(7,100)
