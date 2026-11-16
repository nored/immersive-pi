from types import SimpleNamespace
import pytest
from shared.display_modes import select_mode
from gl_pipeline import KmsDisplay


def test_display_period_uses_pixel_clock_instead_of_rounded_refresh_label():
    display = KmsDisplay()
    timing = SimpleNamespace(clock=83500, htotal=1680, vtotal=831, flags=0, vscan=0)
    display._backend = SimpleNamespace(mode=timing)
    assert display.refresh_period_ns == 16_719_521
    timing.flags = 16
    assert display.refresh_period_ns is None


def mode(w,h,hz=60,flags=0):
    return SimpleNamespace(hdisplay=w,vdisplay=h,vrefresh=hz,flags=flags,
                           clock=0,htotal=0,vtotal=0)


def test_full_hd_request_does_not_use_low_preferred_mode_or_interlacing():
    low,interlaced,full = mode(1280,800),mode(1920,1080,flags=1<<4),mode(1920,1080)
    assert select_mode([low,interlaced,full],'1920x1080@60') is full
    assert select_mode([low,full]) is low
    with pytest.raises(ValueError,match='not advertised'):
        select_mode([low,interlaced],'1920x1080@60')


def test_refresh_selection_avoids_30_hz_and_accepts_5994():
    slow,correct=mode(1920,1080,30),mode(1920,1080,59.94)
    assert select_mode([slow,correct],'1920x1080@60') is correct
