"""scanpattern.py — gray-code bitplane generation on the render node.

Mirrors control-node/autocalib/graycode.make_pattern so the node can render the
exact pattern the control node's decoder expects, in projector pixel space. The
node scans these out RAW (no mesh, no blend) so structured light measures the
real projector, not the warped content. GLES2 has no integer bitwise ops, so the
plane is built on the CPU with numpy and uploaded as a texture.
"""

from __future__ import annotations

import numpy as np


def n_bits(size: int) -> int:
    return int(np.ceil(np.log2(max(size, 2))))


def make_pattern(axis: str, bit: int, inverted: bool, width: int, height: int) -> bytes:
    """Return an RGBA byte buffer (width*height*4) for one gray-code bitplane."""
    if axis is None:  # reference frame: full white or full black
        val = 0 if inverted else 255
        img = np.full((height, width), val, np.uint8)
    else:
        size = width if axis == "x" else height
        coord = np.arange(size, dtype=np.int64)
        g = coord ^ (coord >> 1)
        bits = ((g >> bit) & 1).astype(np.uint8)
        line = np.where(bits == 1, 255, 0).astype(np.uint8)
        if axis == "x":
            img = np.broadcast_to(line[None, :], (height, width)).copy()
        else:
            img = np.broadcast_to(line[:, None], (height, width)).copy()
        if inverted:
            img = 255 - img
    rgba = np.empty((height, width, 4), np.uint8)
    rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = img
    rgba[..., 3] = 255
    return rgba.tobytes()
