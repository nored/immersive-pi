"""solve.py — turn decoded projector↔camera correspondence into a room-model.

Input: for every node, the decode maps in a common camera frame
    proj_x[c], proj_y[c], mask[c]   (camera pixel c -> projector pixel + valid)

A single camera that sees the whole ring defines the common panorama canvas. For
each projector we:

  * bin camera pixels onto the node's NxM output grid (projector space) and take
    the mean camera coordinate at each grid node — that is where that bit of the
    projector lands on the canvas;
  * normalise the camera coordinate over the union of all projector footprints to
    get the source UV the flat content should show there → the mesh control point
    (source uv → output clip xy);
  * find camera pixels lit by two neighbouring projectors → the overlap, whose
    width in output space becomes the per-edge blend width.

Because every projector is placed in the one camera frame, ring closure is
automatic: node 12's right footprint and node 1's left footprint meet on the same
canvas. Manual edits from the calibration tool remain valid on top of the result.

Pure numpy. Self-tests with synthetic correspondence via `python3 solve.py`.
"""

from __future__ import annotations

import numpy as np


def _grid_means(proj_x, proj_y, mask, PW, PH, cols, rows):
    """Mean camera (x,y) at each of cols×rows projector-space grid nodes."""
    ys, xs = np.mgrid[0:mask.shape[0], 0:mask.shape[1]]
    sel = mask
    px = proj_x[sel].astype(np.float64)
    py = proj_y[sel].astype(np.float64)
    cam_x = xs[sel].astype(np.float64)
    cam_y = ys[sel].astype(np.float64)
    gx = np.round(px / max(PW - 1, 1) * (cols - 1)).astype(int)
    gy = np.round(py / max(PH - 1, 1) * (rows - 1)).astype(int)
    gx = np.clip(gx, 0, cols - 1)
    gy = np.clip(gy, 0, rows - 1)
    flat = gy * cols + gx
    n = cols * rows
    cnt = np.bincount(flat, minlength=n).astype(np.float64)
    sx = np.bincount(flat, weights=cam_x, minlength=n)
    sy = np.bincount(flat, weights=cam_y, minlength=n)
    with np.errstate(invalid="ignore", divide="ignore"):
        mx = (sx / cnt).reshape(rows, cols)
        my = (sy / cnt).reshape(rows, cols)
    return mx, my, cnt.reshape(rows, cols)


def _fill_holes(a):
    """Fill NaN grid nodes by nearest valid neighbour then row/col interpolation."""
    a = a.copy()
    if not np.isnan(a).any():
        return a
    # simple iterative neighbour average until filled (grids are tiny)
    for _ in range(a.size):
        nan = np.isnan(a)
        if not nan.any():
            break
        padded = np.pad(a, 1, constant_values=np.nan)
        neigh = np.stack([padded[:-2, 1:-1], padded[2:, 1:-1],
                          padded[1:-1, :-2], padded[1:-1, 2:]])
        with np.errstate(invalid="ignore"):
            avg = np.nanmean(neigh, axis=0)
        a[nan] = avg[nan]
    return a


def _footprint_bbox(mask, proj_x):
    ys, xs = np.where(mask)
    if xs.size == 0:
        return None
    return xs.min(), xs.max(), ys.min(), ys.max()


def solve(scans: dict, cam_shape, PW: int, PH: int,
          cols: int = 4, rows: int = 4, ring: bool = True,
          order: list | None = None):
    """scans: node -> (proj_x, proj_y, mask). Returns list of room-model entries."""
    order = order or sorted(scans.keys())
    CH, CW = cam_shape

    # global panorama bounds = union of all footprints
    boxes = {n: _footprint_bbox(scans[n][2], scans[n][0]) for n in order}
    valid = [b for b in boxes.values() if b]
    gx0 = min(b[0] for b in valid); gx1 = max(b[1] for b in valid)
    gy0 = min(b[2] for b in valid); gy1 = max(b[3] for b in valid)
    span_x = max(gx1 - gx0, 1); span_y = max(gy1 - gy0, 1)

    entries = []
    for i, node in enumerate(order):
        proj_x, proj_y, mask = scans[node]
        mx, my, cnt = _grid_means(proj_x, proj_y, mask, PW, PH, cols, rows)
        mx[cnt == 0] = np.nan; my[cnt == 0] = np.nan
        mx = _fill_holes(mx); my = _fill_holes(my)

        points = []
        for r in range(rows):
            for c in range(cols):
                # source UV = camera coord normalised over the whole panorama
                u = float((mx[r, c] - gx0) / span_x)
                v = float((my[r, c] - gy0) / span_y)
                # output clip = regular projector grid node
                ox = c / (cols - 1) * 2 - 1
                oy = 1 - r / (rows - 1) * 2
                points.append({"u": round(np.clip(u, 0, 1), 5),
                               "v": round(np.clip(v, 0, 1), 5),
                               "x": round(ox, 5), "y": round(oy, 5)})

        blend = _blend_from_overlaps(node, i, order, scans, boxes, ring)
        entries.append({
            "node": node,
            "projector": f"P{i+1}",
            "source_region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "mesh": {"cols": cols, "rows": rows, "points": points},
            "blend": blend,
            "color": {"gain": [1.0, 1.0, 1.0], "gamma": 2.2, "lift": [0.0, 0.0, 0.0]},
        })
    return entries


def _edge(width=0.0, gamma=2.2, black_lift=0.0):
    return {"width": round(float(width), 4), "gamma": gamma,
            "black_lift": round(float(black_lift), 4)}


def _blend_from_overlaps(node, i, order, scans, boxes, ring):
    """Right/left blend widths from the camera-space overlap with neighbours."""
    n = len(order)
    box = boxes[node]
    blend = {"left": _edge(), "right": _edge(), "top": _edge(), "bottom": _edge()}
    if box is None:
        return blend
    x0, x1 = box[0], box[1]
    own_w = max(x1 - x0, 1)

    def overlap_width(other):
        ob = boxes[other]
        if ob is None:
            return 0.0
        lo = max(x0, ob[0]); hi = min(x1, ob[1])
        return max(hi - lo, 0) / own_w

    right_i = i + 1 if i + 1 < n else (0 if ring else None)
    left_i = i - 1 if i - 1 >= 0 else (n - 1 if ring else None)
    if right_i is not None:
        w = overlap_width(order[right_i])
        if w > 0.01:
            blend["right"] = _edge(w, 2.2, 0.04)
    if left_i is not None:
        w = overlap_width(order[left_i])
        if w > 0.01:
            blend["left"] = _edge(w, 2.2, 0.04)
    return blend


def write_into_model(model: dict, entries: list) -> dict:
    """Replace mesh/blend/source_region of matching nodes; keep color + extras."""
    by_node = {e["node"]: e for e in entries}
    for n in model.get("nodes", []):
        e = by_node.get(n["node"])
        if e:
            n["mesh"] = e["mesh"]
            n["blend"] = e["blend"]
            n["source_region"] = e["source_region"]
    # add any solved nodes not present yet
    have = {n["node"] for n in model.get("nodes", [])}
    for e in entries:
        if e["node"] not in have:
            model.setdefault("nodes", []).append(e)
    return model


# ---------------------------------------------------------------------------
# Self-test: synthesize 3 projectors tiling a camera panorama with overlaps,
# build correspondence directly, solve, and check meshes + blends.
# ---------------------------------------------------------------------------
def _self_test():
    CW, CH = 600, 120
    PW, PH = 256, 128
    N = 3
    overlap = 0.10                      # 10% camera overlap between neighbours
    seg = CW / (N - overlap * (N - 1))  # each projector's camera width

    scans = {}
    for i in range(N):
        node = f"pi-{i+1:02d}"
        cam_x0 = i * (seg * (1 - overlap))
        cam_x1 = cam_x0 + seg
        proj_x = np.full((CH, CW), -1, np.int64)
        proj_y = np.full((CH, CW), -1, np.int64)
        mask = np.zeros((CH, CW), bool)
        xs = np.arange(CW)
        inside = (xs >= cam_x0) & (xs < min(cam_x1, CW))
        # map this camera band linearly to projector columns; rows map straight
        for cx in xs[inside]:
            px = int((cx - cam_x0) / max(cam_x1 - cam_x0, 1) * (PW - 1))
            proj_x[:, cx] = np.clip(px, 0, PW - 1)
        proj_y[:, :] = (np.arange(CH)[:, None] / (CH - 1) * (PH - 1)).astype(np.int64)
        mask[:, inside] = True
        scans[node] = (proj_x, proj_y, mask)

    entries = solve(scans, (CH, CW), PW, PH, cols=4, rows=4, ring=False)
    assert len(entries) == 3

    # node 1 center column should map near panorama u≈ its center; nodes ordered
    centers = []
    for e in entries:
        pts = e["mesh"]["points"]
        mid = np.mean([p["u"] for p in pts])
        centers.append(mid)
    assert centers[0] < centers[1] < centers[2], centers
    # interior nodes overlap both sides; ends overlap one side
    b1 = entries[1]["blend"]
    assert b1["left"]["width"] > 0.05 and b1["right"]["width"] > 0.05, b1
    assert entries[0]["blend"]["left"]["width"] == 0.0   # strip end, no left
    assert entries[2]["blend"]["right"]["width"] == 0.0  # strip end, no right
    # full source coverage: union of node u-spans covers ~[0,1]
    umin = min(min(p["u"] for p in e["mesh"]["points"]) for e in entries)
    umax = max(max(p["u"] for p in e["mesh"]["points"]) for e in entries)
    assert umin < 0.05 and umax > 0.95, (umin, umax)   # small inset = grid binning
    print(f"solve self-test OK — 3 meshes ordered u={[round(c,2) for c in centers]}, "
          f"interior blends L={b1['left']['width']} R={b1['right']['width']}, "
          f"panorama coverage [{umin:.3f},{umax:.3f}]")


if __name__ == "__main__":
    _self_test()
