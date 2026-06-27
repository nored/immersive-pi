"""graycode.py — gray-code structured-light patterns and decode.

Each projector shows a stack of black/white stripe patterns (one per bit, plus
its inverse for robust per-pixel thresholding), in X then Y. A single camera
captures the stack. Decoding recovers, for every camera pixel, which projector
column and row illuminated it — the projector→camera correspondence the solver
turns into meshes and blend masks.

Patterns are generated on the CPU as images (GLES2 has no integer bitwise ops,
so we don't compute gray code in the shader; the node uploads each image and
scans it out raw, bypassing the warp). Pure numpy — runs and self-tests with no
camera via `python3 graycode.py`.
"""

from __future__ import annotations

import numpy as np


def n_bits(size: int) -> int:
    return int(np.ceil(np.log2(max(size, 2))))


def _gray(values: np.ndarray) -> np.ndarray:
    """Binary -> reflected Gray code."""
    return values ^ (values >> 1)


def _inverse_gray(g: np.ndarray) -> np.ndarray:
    """Reflected Gray code -> binary."""
    b = g.copy()
    shift = 1
    while (1 << shift) <= int(b.max()) if b.size else False:
        b ^= g >> shift
        shift += 1
    # robust fixed-width version (covers all bits up to 16)
    b = g.copy()
    for s in range(1, 16):
        b ^= (g >> s)
    return b


def pattern_count(width: int, height: int) -> int:
    """Total frames in a scan: (x bits + y bits) * 2 (normal+inverse) + 2 refs."""
    return (n_bits(width) + n_bits(height)) * 2 + 2


def make_pattern(axis: str, bit: int, inverted: bool, width: int, height: int) -> np.ndarray:
    """One gray-code bitplane as a HxWx3 uint8 image (white where the bit is set)."""
    if axis == "x":
        coord = np.arange(width, dtype=np.int64)
        g = _gray(coord)
        bits = ((g >> bit) & 1).astype(np.uint8)           # (W,)
        row = np.where(bits == 1, 255, 0).astype(np.uint8)
        img = np.broadcast_to(row[None, :], (height, width)).copy()
    else:
        coord = np.arange(height, dtype=np.int64)
        g = _gray(coord)
        bits = ((g >> bit) & 1).astype(np.uint8)           # (H,)
        col = np.where(bits == 1, 255, 0).astype(np.uint8)
        img = np.broadcast_to(col[:, None], (height, width)).copy()
    if inverted:
        img = 255 - img
    return np.repeat(img[:, :, None], 3, axis=2)


def scan_sequence(width: int, height: int):
    """Yield (name, axis, bit, inverted) for a full scan, references first."""
    yield ("white", None, None, False)
    yield ("black", None, None, True)
    for axis, size in (("x", width), ("y", height)):
        for bit in range(n_bits(size)):
            yield (f"{axis}{bit}", axis, bit, False)
            yield (f"{axis}{bit}i", axis, bit, True)


def decode(captures: dict, width: int, height: int, contrast: int = 12):
    """Decode a captured stack into projector-coordinate maps.

    captures: name -> grayscale HxW float/uint8 camera image (keys from
              scan_sequence). Returns (proj_x, proj_y, mask) in camera space:
      proj_x[c]  projector column 0..width-1   illuminating camera pixel c
      proj_y[c]  projector row    0..height-1
      mask[c]    True where the pixel had enough contrast to trust
    """
    white = captures["white"].astype(np.float32)
    black = captures["black"].astype(np.float32)
    mask = (white - black) > contrast
    mid = (white + black) * 0.5

    def decode_axis(axis, size):
        nb = n_bits(size)
        g = np.zeros(white.shape, dtype=np.int64)
        for bit in range(nb):
            normal = captures[f"{axis}{bit}"].astype(np.float32)
            inv = captures[f"{axis}{bit}i"].astype(np.float32)
            # a pixel's bit is 1 where the normal pattern is brighter than its
            # inverse; using both rejects ambient/albedo per pixel
            b = (normal > inv).astype(np.int64)
            # MSB-first: bit 0 is the LSB of the gray code
            g |= (b << bit)
        return _inverse_gray(g)

    proj_x = decode_axis("x", width)
    proj_y = decode_axis("y", height)
    proj_x = np.clip(proj_x, 0, width - 1)
    proj_y = np.clip(proj_y, 0, height - 1)
    return proj_x, proj_y, mask


# ---------------------------------------------------------------------------
# Self-test: synthesize a camera that sees a projector through a known mapping,
# decode it, and confirm we recover the projector coordinates.
# ---------------------------------------------------------------------------
def _self_test():
    PW, PH = 256, 128          # projector resolution (small for speed)
    CW, CH = 200, 100          # camera resolution

    # Known synthetic mapping: each camera pixel sees projector coord via an
    # affine warp + a little barrel-ish bend, so it's not the identity.
    cy, cx = np.mgrid[0:CH, 0:CW].astype(np.float32)
    u = cx / (CW - 1)
    v = cy / (CH - 1)
    bend = 0.06 * np.sin(v * np.pi)
    true_px = np.clip(((u + bend) * (PW - 1)), 0, PW - 1).astype(np.int64)
    true_py = np.clip((v * (PH - 1)), 0, PH - 1).astype(np.int64)

    # Build camera captures by sampling each projected pattern at the mapping,
    # with per-pixel albedo + ambient to prove the normal/inverse decode is robust.
    albedo = 0.5 + 0.5 * np.random.RandomState(0).rand(CH, CW).astype(np.float32)
    ambient = 18.0
    captures = {}
    for name, axis, bit, inv in scan_sequence(PW, PH):
        if axis is None:
            proj = np.full((PH, PW), 0 if inv else 255, np.uint8)
        else:
            proj = make_pattern(axis, bit, inv, PW, PH)[:, :, 0]
        seen = proj[true_py, true_px].astype(np.float32)
        captures[name] = np.clip(seen * albedo + ambient, 0, 255)

    px, py, mask = decode(captures, PW, PH)
    err_x = np.abs(px[mask] - true_px[mask])
    err_y = np.abs(py[mask] - true_py[mask])
    print(f"coverage {mask.mean()*100:.1f}%  "
          f"x err: median {np.median(err_x):.1f}px max {err_x.max()}  "
          f"y err: median {np.median(err_y):.1f}px max {err_y.max()}")
    assert mask.mean() > 0.99
    assert np.median(err_x) <= 1 and np.median(err_y) <= 1
    print("graycode self-test OK — projector coords recovered through albedo+ambient")


if __name__ == "__main__":
    _self_test()
