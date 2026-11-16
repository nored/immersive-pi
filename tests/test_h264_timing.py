"""Guard the hardware encoder's missing timing and lossless metadata repair."""
from fractions import Fraction
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

av = pytest.importorskip('av')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/phone-capture'))
from repair_h264_timing import add_sps_timing, packet_nals, repair


def test_real_videotoolbox_sps_repair_is_idempotent():
    original = bytes.fromhex('27640020ac5680500659')
    fixed, changed = add_sps_timing(original, Fraction(521875, 17451))
    assert changed and fixed != original
    assert add_sps_timing(fixed, Fraction(521875, 17451)) == (fixed, False)
    assert add_sps_timing(b'\x65picture', Fraction(30)) == (b'\x65picture', False)


def test_truncated_packet_is_rejected():
    with pytest.raises(ValueError):
        list(packet_nals(b'\x00\x00\x00\x08short', 4))


@pytest.mark.skipif(sys.platform != 'darwin' or not shutil.which('ffmpeg'),
                    reason='Requires Apple VideoToolbox and ffmpeg')
def test_metadata_repair_preserves_decoded_pixels_and_timestamps(tmp_path):
    source = tmp_path / 'source.mp4'
    destination = tmp_path / 'fixed.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=128x80:rate=30', '-frames:v', '12',
                    '-c:v', 'h264_videotoolbox', '-b:v', '1M', str(source)], check=True)
    result = repair(source, destination, Fraction(30))
    assert result['compressed_picture_data_unchanged'] and result['packets'] == 12
    def pictures(path):
        with av.open(str(path)) as container:
            return [(frame.pts, frame.time_base, frame.to_ndarray(format='rgb24').tobytes())
                    for frame in container.decode(video=0)]
    assert pictures(source) == pictures(destination)
