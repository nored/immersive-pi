import importlib.util
from fractions import Fraction
from pathlib import Path


spec = importlib.util.spec_from_file_location('library_convert',
    Path(__file__).resolve().parents[1]/'tools/media-library/convert.py')
converter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)


def test_matroska_audio_tail_does_not_extend_the_picture_track():
    metadata = {'streams':[{'start_time':'0.007000',
        'tags':{'DURATION':'00:00:49.767000000'}}], 'format':{'duration':'50.561000'}}
    assert converter.video_duration(metadata) == Fraction('49.760')
    assert int(converter.video_duration(metadata)*Fraction(521875,17451)) == 1488


def test_explicit_video_duration_wins_over_longer_container():
    metadata = {'streams':[{'start_time':'2','duration':'10'}], 'format':{'duration':'15'}}
    assert converter.video_duration(metadata) == 10
