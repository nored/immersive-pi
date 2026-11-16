"""corner.py: seam measurement where two projectors overlap only across a wall
corner (markers there are folded and unreadable).

Each projector's markers on its own wall give a plane mapping (camera ->
projector). The projector's sheet, predicted through that mapping, matches the
camera image only up to the corner; the line where projector A's agreement ends
and projector B's begins is the corner. Points on that line lie on both walls,
so A's and B's mappings are both valid there: those are the seam pairs.
"""
from __future__ import annotations

import cv2
import numpy as np

import markers

SCALE = 0.5          # work at half resolution
WIN = 15             # local correlation window (px at working scale)


def unmix(img, lay, det_by_index):
    """Per-projector-colour images using colours measured in this frame."""
    f = img.astype(np.float32)
    black = np.percentile(f.reshape(-1, 3), 3, axis=0)
    cols = {}
    for k, (ids, cs) in det_by_index.items():
        colour = lay["nodes"][k]["colour"]
        for c in cs:
            m = np.zeros(f.shape[:2], np.uint8)
            cv2.fillConvexPoly(m, np.round(c).astype(np.int32), 1)
            px = f[m > 0]
            if len(px):
                lum = px.sum(1); cols.setdefault(colour, []).append(px[lum >= np.percentile(lum, 70)])
    M = np.zeros((3, 3), np.float32)
    default = {0: [0, 0, 200], 1: [0, 200, 0], 2: [200, 0, 0]}
    for colour in range(3):
        v = (np.median(np.concatenate(cols[colour]), 0) - black) if colour in cols else np.array(default[colour], np.float32)
        M[:, colour] = np.maximum(v, 1)
    un = (f.reshape(-1, 3) - black) @ np.linalg.pinv(M).T
    return un.reshape(f.shape)


def plane_h(lay, k, ids, cs):
    nd = lay["nodes"][k]
    src = np.concatenate([c for c in cs]).astype(np.float32)
    dst = np.concatenate([np.array(nd["markers"][int(i)], np.float32) / [markers.PW, markers.PH] for i in ids]).astype(np.float32)
    H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 0.01)
    return H, (inl.ravel() > 0) if inl is not None else None


def local_ncc(a, b, win=WIN):
    mu_a = cv2.blur(a, (win, win)); mu_b = cv2.blur(b, (win, win))
    va = cv2.blur(a * a, (win, win)) - mu_a ** 2; vb = cv2.blur(b * b, (win, win)) - mu_b ** 2
    cov = cv2.blur(a * b, (win, win)) - mu_a * mu_b
    return cov / np.sqrt(np.maximum(va * vb, 1e-6))


def corner_pairs(img, lay, sheets, ka, kb, det_by_index, debug=None):
    """Return (pa, pb) normalised projector coords along the corner line, or None."""
    ia, ca = det_by_index[ka]; ib, cb = det_by_index[kb]
    if len(ia) < 6 or len(ib) < 6:
        return None
    Ha, inA = plane_h(lay, ka, ia, ca); Hb, inB = plane_h(lay, kb, ib, cb)
    if Ha is None or Hb is None or inA.sum() < 16 or inB.sum() < 16:
        return None
    small = cv2.resize(img, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_AREA)
    h, w = small.shape[:2]
    un = unmix(small, lay, {k: (v[0], v[1] * SCALE) for k, v in det_by_index.items()})
    S = np.diag([SCALE, SCALE, 1.0]); Sinv = np.linalg.inv(S)
    N = np.diag([markers.PW, markers.PH, 1.0])
    pred, agree = {}, {}
    for k, H in ((ka, Ha), (kb, Hb)):
        # camera(small) -> projector pixels; warp the sheet into the camera frame
        Hc = N @ H @ Sinv
        pred[k] = cv2.warpPerspective(sheets[k], np.linalg.inv(Hc), (w, h), flags=cv2.INTER_LINEAR,
                                      borderValue=-1).astype(np.float32)
        ch = un[..., lay["nodes"][k]["colour"]]
        inside = cv2.erode((pred[k] >= 0).astype(np.uint8), np.ones((WIN + 4, WIN + 4), np.uint8)) > 0
        agree[k] = np.where(inside, local_ncc(np.maximum(pred[k], 0), ch), -1)
    # search band between A's rightmost marker and B's leftmost marker (+ margin)
    xa = (np.concatenate(ca)[:, 0] * SCALE); xb = (np.concatenate(cb)[:, 0] * SCALE)
    left_to_right = np.median(xa) < np.median(xb)
    lo = int(max(0, min(np.percentile(xa, 90), np.percentile(xb, 10)) - 60))
    hi = int(min(w, max(np.percentile(xa, 90), np.percentile(xb, 10)) + 60))
    ya = np.concatenate([np.concatenate(ca)[:, 1], np.concatenate(cb)[:, 1]]) * SCALE
    y0, y1 = int(max(0, ya.min())), int(min(h, ya.max()))
    pts = []
    for y in range(y0, y1, 6):
        A = agree[ka][y, lo:hi]; B = agree[kb][y, lo:hi]
        if left_to_right:
            okA = A > 0.45; okB = B > 0.45
        else:
            okA, okB = A[::-1] > 0.45, B[::-1] > 0.45
        # A: last x of the run of agreement coming from its own side; B: first x of its run
        if not okA.any() or not okB.any():
            continue
        endA = len(okA) - 1 - np.argmax(okA[::-1])           # rightmost agreeing pixel of A
        startB = np.argmax(okB)                              # leftmost agreeing pixel of B
        if abs(endA - startB) > 25:
            continue
        x = (endA + startB) / 2
        x = lo + (x if left_to_right else (hi - lo - 1 - x))
        pts.append((x, y))
    if len(pts) < 8:
        return None
    pts = np.array(pts, np.float32)
    # the corner is a straight line in the image: robust line fit x = a*y + b
    best = None
    rng = np.random.default_rng(0)
    for _ in range(200):
        i, j = rng.choice(len(pts), 2, replace=False)
        if pts[i, 1] == pts[j, 1]:
            continue
        a = (pts[j, 0] - pts[i, 0]) / (pts[j, 1] - pts[i, 1]); b = pts[i, 0] - a * pts[i, 1]
        inl = np.abs(pts[:, 0] - (a * pts[:, 1] + b)) < 4
        if best is None or inl.sum() > best[0]:
            best = (inl.sum(), inl)
    inl = best[1]
    if inl.sum() < 8:
        return None
    a, b = np.polyfit(pts[inl, 1], pts[inl, 0], 1)
    ys = np.arange(pts[inl, 1].min(), pts[inl, 1].max(), 2.0)
    line = np.stack([a * ys + b, ys], 1) / SCALE                 # full-resolution camera coords
    pa = cv2.perspectiveTransform(line[None].astype(np.float32), Ha)[0]
    pb = cv2.perspectiveTransform(line[None].astype(np.float32), Hb)[0]
    ok = np.all((pa >= 0) & (pa <= 1), 1) & np.all((pb >= 0) & (pb <= 1), 1)
    if debug is not None:
        debug.update(line=line, agreeA=agree[ka], agreeB=agree[kb], band=(lo, hi), pts=pts, inl=inl)
    if ok.sum() < 8:
        return None
    return pa[ok], pb[ok]


def load_sheets(lay, frame_png):
    """Each node's sheet at projector resolution, grey, as projected."""
    frame = cv2.imread(str(frame_png))
    out = {}
    for k, nd in enumerate(lay["nodes"]):
        x0, y0, tw, th = nd["tile_px"]
        tile = frame[y0:y0 + th, x0:x0 + tw].max(axis=2)
        out[k] = cv2.resize(tile, (markers.PW, markers.PH), interpolation=cv2.INTER_LINEAR).astype(np.float32)
    return out
