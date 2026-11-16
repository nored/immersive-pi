"""floorline.py: real top and bottom border of each projector's picture.

Bottom: walking down each projector column from the middle of the picture,
  * if the projected sheet stops fitting the projector's wall plane while still
    inside the picture, and the image below is dark and flat, the picture runs
    onto the floor: the fold is the floor line;
  * if the walk reaches the picture's own bottom edge first, the picture ends
    on the wall: the border is the bottom row (y = 1).
Top: the same walking up (rail / ceiling fold, or the top row y = 0).

The local comparison uses a window of corner.WIN pixels; a fold is detected
when the window starts to overlap it, so the position is corrected by half a
window, converted to projector units along the path."""
from __future__ import annotations

import cv2
import numpy as np

import corner
import markers

COLS = np.linspace(0.06, 0.94, 12)


def _prepare(img, lay, det_by_index, sheets, min_markers):
    cand = {k: v for k, v in det_by_index.items() if len(v[0]) >= min_markers}
    if not cand or not sheets:
        return None
    SC = corner.SCALE
    small = cv2.resize(img, None, fx=SC, fy=SC, interpolation=cv2.INTER_AREA)
    h, w = small.shape[:2]
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32), (5, 5), 0)
    un = corner.unmix(small, lay, {k: (v[0], v[1] * SC) for k, v in det_by_index.items()})
    N = np.diag([markers.PW, markers.PH, 1.0]); Sinv = np.linalg.inv(np.diag([SC, SC, 1.0]))
    per = {}
    for k, (ids, cs) in cand.items():
        H, inl = corner.plane_h(lay, k, ids, cs)
        if H is None or inl.sum() < 3 * min_markers:
            continue
        pred = cv2.warpPerspective(sheets[k], np.linalg.inv(N @ H @ Sinv), (w, h), flags=cv2.INTER_LINEAR, borderValue=-1)
        # window fully inside the picture -> comparison valid
        valid = cv2.erode((pred >= 0).astype(np.uint8), np.ones((corner.WIN, corner.WIN), np.uint8)) > 0
        ag = corner.local_ncc(np.maximum(pred, 0).astype(np.float32), un[..., lay["nodes"][k]["colour"]].astype(np.float32))
        ag = np.where(valid, ag, np.nan).astype(np.float32)
        per[k] = (H, ag)
    return {"SC": SC, "w": w, "h": h, "gray": gray, "per": per}


def _walk(prep, k, x, ys, need_dark):
    """Return the border y along column x walking through ys, or None."""
    SC, w, h, gray = prep["SC"], prep["w"], prep["h"], prep["gray"]
    H, ag = prep["per"][k]
    pts = np.stack([np.full_like(ys, x), ys], 1).astype(np.float32)
    cam = cv2.perspectiveTransform(pts[None], np.linalg.inv(H))[0] * SC
    ok = (cam[:, 0] >= 1) & (cam[:, 0] < w - 2) & (cam[:, 1] >= 1) & (cam[:, 1] < h - 2)
    if ok[:60].mean() < 0.9:
        return None
    mx = cam[:, 0].reshape(1, -1).astype(np.float32); my = cam[:, 1].reshape(1, -1).astype(np.float32)
    agp = cv2.remap(ag, mx, my, cv2.INTER_NEAREST, borderValue=np.nan)[0]
    gp = cv2.remap(gray, mx, my, cv2.INTER_LINEAR, borderValue=np.nan)[0]
    if np.nanmean(agp[:60] > 0.45) < 0.6:
        return None                                   # wall not confirmed here
    step_px = np.median(np.linalg.norm(np.diff(cam, axis=0), axis=1))
    half_win = (corner.WIN / 2) / max(step_px, 1e-6) * abs(ys[1] - ys[0])
    sgn = 1.0 if ys[-1] > ys[0] else -1.0
    for i in range(60, len(ys) - 20):
        if not ok[i]:
            return None
        if np.isnan(agp[i]):
            # reached the picture's own edge without a fold before it
            return 1.0 if sgn > 0 else 0.0
        seg = agp[i:i + 20]
        if np.mean((seg < 0.3) & ~np.isnan(seg)) > 0.8:
            if need_dark:
                above, below = gp[i - 30:i], gp[i:i + 30]
                if np.isnan(below).any() or not (np.nanmean(below) < 0.55 * np.nanmean(above)):
                    continue
            return float(ys[i] + sgn * half_win)
    return None


def _wall_top_above(prep, k, x):
    """Upper end of the wall above a picture that ends on the wall: extend the
    projector's wall plane upwards; the wall's end is where white wall (lighter,
    below) turns into ceiling (darker, above) and the ceiling then stays
    uniform. A rail lip (dark line with white above it) does not qualify."""
    SC, w, h, gray = prep["SC"], prep["w"], prep["h"], prep["gray"]
    H, _ = prep["per"][k]
    ys = np.linspace(-0.02, -0.6, 291)
    pts = np.stack([np.full_like(ys, x), ys], 1).astype(np.float32)
    cam = cv2.perspectiveTransform(pts[None], np.linalg.inv(H))[0] * SC
    ok = (cam[:, 0] >= 1) & (cam[:, 0] < w - 2) & (cam[:, 1] >= 1) & (cam[:, 1] < h - 2)
    g = cv2.remap(gray, cam[:, 0].reshape(1, -1).astype(np.float32), cam[:, 1].reshape(1, -1).astype(np.float32),
                  cv2.INTER_LINEAR, borderValue=np.nan)[0]
    g[~ok] = np.nan
    best, best_i = 0.0, None
    for i in range(8, len(ys) - 40):
        below, above, plateau = g[i - 8:i], g[i:i + 8], g[i:i + 40]
        if np.isnan(below).any() or np.isnan(plateau).any():
            continue
        step = below.mean() - above.mean()          # wall lighter than ceiling
        if step < 5:
            continue
        score = step / (1.0 + np.std(plateau))      # ceiling uniform above the edge
        if score > best:
            best, best_i = score, i
    if best_i is None or best < 0.8:
        return None
    return float(ys[best_i])


def _floor_edge(prep, k, x):
    """Floor line near the picture's bottom, to sub-percent precision.
    Below the last marker row the sheet is plain lit margin; walking down, the
    only light-to-dark step there is wall -> floor (the picture may run onto the
    floor, or end above it). Marker edges in that range go dark-to-light."""
    SC, w, h, gray = prep["SC"], prep["w"], prep["h"], prep["gray"]
    H, _ = prep["per"][k]
    ys = np.linspace(0.90, 1.10, 201)
    pts = np.stack([np.full_like(ys, x), ys], 1).astype(np.float32)
    cam = cv2.perspectiveTransform(pts[None], np.linalg.inv(H))[0] * SC
    ok = (cam[:, 0] >= 1) & (cam[:, 0] < w - 2) & (cam[:, 1] >= 1) & (cam[:, 1] < h - 2)
    if ok.mean() < 0.95:
        return None
    g = cv2.remap(gray, cam[:, 0].reshape(1, -1).astype(np.float32), cam[:, 1].reshape(1, -1).astype(np.float32),
                  cv2.INTER_LINEAR, borderValue=np.nan)[0]
    best, bi = 0.0, None
    for i in range(8, len(ys) - 15):
        above, below = g[i - 8:i], g[i:i + 15]
        if np.isnan(above).any() or np.isnan(below).any():
            continue
        step = above.mean() - below.mean()
        # floor: clearly darker than the wall above, and stays dark below
        if step > 15 and below.mean() < 0.6 * above.mean() and step > best:
            best, bi = step, i
    if bi is None:
        return None
    # sub-sample position: steepest point of the step
    d = np.abs(np.gradient(g[max(0, bi - 6):bi + 7]))
    return float(ys[max(0, bi - 6) + int(np.nanargmax(d))])


def _fit(res):
    if len(res) < 4:
        return None
    arr = np.array(res)
    a, b = np.polyfit(arr[:, 0], arr[:, 1], 1)
    keep = np.abs(arr[:, 1] - (a * arr[:, 0] + b)) < 0.02
    return arr[keep] if keep.sum() >= 4 else None


def borders(img, lay, det_by_index, sheets=None, min_markers=6, xrange=None):
    """{k: {"floor": points or None, "top": points or None}} in projector units.
    xrange: {k: (lo, hi)} columns on the projector's own wall (beyond a wall
    corner the floor and the wall top lie on another wall)."""
    prep = _prepare(img, lay, det_by_index, sheets, min_markers)
    if prep is None:
        return {}
    out = {}
    down = np.linspace(0.5, 1.15, 261); up = np.linspace(0.5, -0.15, 261)
    for k in prep["per"]:
        lo, hi = (xrange or {}).get(k, (0.0, 1.0))
        cols = [x for x in COLS if lo + 0.03 <= x <= hi - 0.03]
        if len(cols) < 4:
            continue
        fl = _fit([(x, y) for x in cols for y in [_floor_edge(prep, k, x)] if y is not None])
        tops, walls = [], []
        for x in cols:
            y = _walk(prep, k, x, up, False)
            if y is not None:
                tops.append((x, y))
            if y == 0.0:                          # picture ends on the wall: find the wall's upper end
                yw = _wall_top_above(prep, k, x)
                if yw is not None:
                    walls.append((x, yw))
        tp = _fit(tops)
        wt = _fit(walls)
        if fl is not None or tp is not None or wt is not None:
            out[k] = {"floor": fl, "top": tp, "walltop": wt}
    return out


def floor_samples(img, lay, det_by_index, sheets=None, min_markers=6):
    return {k: v["floor"] for k, v in borders(img, lay, det_by_index, sheets, min_markers).items() if v["floor"] is not None}
