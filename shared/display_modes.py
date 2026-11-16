"""Select an advertised progressive display mode without a silent downgrade."""
import re


def refresh(mode):
    if mode.clock and mode.htotal and mode.vtotal:
        return mode.clock*1000/(mode.htotal*mode.vtotal)
    return float(mode.vrefresh)


def select_mode(modes, requested=None):
    if not modes:
        raise ValueError('display has no advertised modes')
    if not requested or requested == 'preferred':
        return modes[0]
    match = re.fullmatch(r'(\d+)x(\d+)@(\d+)', requested)
    if not match:
        raise ValueError('display mode must be WIDTHxHEIGHT@REFRESH or preferred')
    width,height,hz = map(int,match.groups())
    candidates = [m for m in modes if m.hdisplay==width and m.vdisplay==height
                  and not m.flags & ((1<<4)|(1<<5)) and abs(refresh(m)-hz)<=1]
    if not candidates:
        raise ValueError(f'requested {requested} progressive mode is not advertised; check projector power, HDMI and EDID')
    return min(candidates,key=lambda m:abs(refresh(m)-hz))
