"""solve_wall.py: output correction for n fixed projectors from seam correspondences.

Input: for every seam (A, B) between neighbouring nodes, pairs of normalised
projector coordinates (pA, pB) that land on the same spot of the wall, as
measured by the camera from the projected markers.

Each projector P gets content coordinates U_P(x, y), V_P(x, y) (x, y in 0..1
over its own output) as smooth polynomials. One linear least-squares solve
makes every seam agree (U_A(pA) = U_B(pB), V too) while keeping each
projector's content as even as possible in its own pixel grid. A ring closes
with U_last = U_first + 1 across the last seam; an open row pins its two ends.

From the solved maps: per node a content strip [U0, U1] (inner edges, so no
black gaps), a dense mesh (content uv -> output clip xy), blend widths from the
overlap with each neighbour in content space, and the common vertical band.
"""
from __future__ import annotations

import json, math
import numpy as np
from scipy.interpolate import griddata, RBFInterpolator

DX, DY = 3, 2                 # polynomial degree in x and y
TERMS = [(i, j) for i in range(DX + 1) for j in range(DY + 1)]
NT = len(TERMS)
import os
SMOOTH = float(os.environ.get('SMOOTH', '1e-4'))
LEVEL = float(os.environ.get('LEVEL', '1.0'))
HEIGHT = float(os.environ.get('HEIGHT', '2.0'))
CORNER_VW = float(os.environ.get('CORNER_VW', '0.05'))
V_ROWS = os.environ.get('V_ROWS', '1') == '1'


def basis(x, y):
    X = 2 * np.asarray(x, float) - 1; Y = 2 * np.asarray(y, float) - 1
    return np.stack([X ** i * Y ** j for i, j in TERMS], -1)


def dbasis(x, y, wrt, order=1):
    """Derivative of the basis (w.r.t. normalised 0..1 coords)."""
    X = 2 * np.asarray(x, float) - 1; Y = 2 * np.asarray(y, float) - 1
    cols = []
    for i, j in TERMS:
        if wrt == "x":
            k = i
            c = math.prod(range(k - order + 1, k + 1)) if k >= order else 0
            cols.append(c * X ** max(k - order, 0) * Y ** j * 2 ** order)
        elif wrt == "y":
            k = j
            c = math.prod(range(k - order + 1, k + 1)) if k >= order else 0
            cols.append(c * Y ** max(k - order, 0) * X ** i * 2 ** order)
        else:  # mixed xy
            c = i * j
            cols.append(c * X ** max(i - 1, 0) * Y ** max(j - 1, 0) * 4)
    return np.stack(cols, -1)


def _solve_core(order, ring, seams, reg=0.3, mesh=(11, 7), blend_gamma=2.2, corner_seams=(), floors=None, borders=None):
    """order: node list; seams: {(A, B): (pA (N,2), pB (N,2))} normalised coords.
    Returns (edits, report, model)."""
    nodes = [n for n in order if any(n in s for s in seams)]
    idx = {n: k for k, n in enumerate(nodes)}
    N = len(nodes)
    nU = N * NT
    rows, rhs, wts = [], [], []

    def eq(coefs, b, w):
        rows.append(coefs); rhs.append(b); wts.append(w)

    first, last = order[0], order[-1]
    for (A, B), (pa, pb) in seams.items():
        if A not in idx or B not in idx or len(pa) == 0:
            continue
        ba, bb = basis(pa[:, 0], pa[:, 1]), basis(pb[:, 0], pb[:, 1])
        wrap = ring and A == last and B == first
        w = 1.0 / math.sqrt(len(pa))
        # across a wall corner the camera measures the horizontal join well but
        # not the vertical scale (points on a folded line): weight V down there
        wv = CORNER_VW if (A, B) in corner_seams else 1.0
        for k in range(len(pa)):
            for comp in (0, 1):   # 0: U, 1: V
                c = np.zeros(2 * nU)
                off = comp * nU
                c[off + idx[A] * NT: off + (idx[A] + 1) * NT] = ba[k]
                c[off + idx[B] * NT: off + (idx[B] + 1) * NT] -= bb[k]
                eq(c, (1.0 if (wrap and comp == 0) else 0.0), w * 30 * (wv if comp == 1 else 1.0))

    # regularisation: smooth, even content in each projector's own grid
    gx, gy = np.meshgrid(np.linspace(0, 1, 7), np.linspace(0, 1, 5))
    gx, gy = gx.ravel(), gy.ravel()
    for n in nodes:
        o = idx[n] * NT
        for comp in (0, 1):
            off = comp * nU + o
            for D in (dbasis(gx, gy, "x", 2), dbasis(gx, gy, "y", 2), dbasis(gx, gy, "xy")):
                for r in D:
                    c = np.zeros(2 * nU); c[off:off + NT] = r; eq(c, 0.0, reg)
    # level: each projector is mounted roughly level, so its pixel rows are the
    # horizontal reference. Without this a closed ring may tilt as a whole
    # (seams still agree) and the common content band collapses.
    dvx = dbasis(gx, gy, "x").mean(0)
    for n in nodes:
        c = np.zeros(2 * nU); c[nU + idx[n] * NT: nU + (idx[n] + 1) * NT] = dvx
        eq(c, 0.0, LEVEL)
    # same picture height for every projector (same model, similar mounting);
    # the walls confirm 1:1 between neighbours, this carries it across corners
    for n in nodes:
        c = np.zeros(2 * nU); o = nU + idx[n] * NT
        c[o:o + NT] = basis(0.5, 1.0) - basis(0.5, 0.0)
        eq(c, 1.0, HEIGHT)
    # global shear: on average, vertical lines stay vertical in projector space
    c = np.zeros(2 * nU)
    for n in nodes:
        c[idx[n] * NT: (idx[n] + 1) * NT] = dbasis(gx, gy, "y").mean(0)
    eq(c, 0.0, 1.0)
    if not ring:
        c = np.zeros(2 * nU)
        for n in nodes:
            c[nU + idx[n] * NT: nU + (idx[n] + 1) * NT] = dbasis(gx, gy, "x").mean(0)
        eq(c, 0.0, 1.0)
    # gauge: U = 0 at the middle of the first node, V = 0 / 1 at its top / bottom centre
    n0 = nodes[0]
    c = np.zeros(2 * nU); c[idx[n0] * NT:(idx[n0] + 1) * NT] = basis(0.5, 0.5); eq(c, 0.0, 10)
    c = np.zeros(2 * nU); c[nU + idx[n0] * NT:nU + (idx[n0] + 1) * NT] = basis(0.5, 0.0); eq(c, 0.0, 10)
    c = np.zeros(2 * nU); c[nU + idx[n0] * NT:nU + (idx[n0] + 1) * NT] = basis(0.5, 1.0); eq(c, 1.0, 10)
    if not ring:   # open row: last node's middle defines the scale (rescaled below)
        nl = nodes[-1]
        c = np.zeros(2 * nU); c[idx[nl] * NT:(idx[nl] + 1) * NT] = basis(0.5, 0.5); eq(c, float(len(nodes) - 1), 10)

    Aq = np.array(rows) * np.array(wts)[:, None]
    bq = np.array(rhs) * np.array(wts)
    sol, *_ = np.linalg.lstsq(Aq, bq, rcond=None)
    CU = {n: sol[idx[n] * NT:(idx[n] + 1) * NT] for n in nodes}
    CV = {n: sol[nU + idx[n] * NT: nU + (idx[n] + 1) * NT] for n in nodes}
    borders = borders or {}
    fl = None
    if True:
        # vertical from the measured borders: top (wall top fold, or the picture's
        # top row) -> 0, floor (floor fold, or the bottom row) -> 1, along each
        # projector column; rows outside stay black
        fl = True
        gxv, gyv = np.meshgrid(np.linspace(0, 1, 9), np.linspace(0, 1, 7))
        gxv, gyv = gxv.ravel(), gyv.ravel()
        Bv = basis(gxv, gyv)
        for n in nodes:
            ta, tb, fa, fb = borders.get(n, (0.0, 0.0, 0.0, 1.0))
            top = ta * gxv + tb; bot = fa * gxv + fb
            CV[n] = np.linalg.lstsq(Bv, (gyv - top) / np.maximum(bot - top, 0.2), rcond=None)[0]
    if not fl and V_ROWS:
        # vertical follows each projector's own pixel rows (full height, level
        # lines); only local corrections at flat seams below
        rows_v = np.zeros(NT); rows_v[TERMS.index((0, 0))] = 0.5; rows_v[TERMS.index((0, 1))] = 0.5
        CV = {n: rows_v.copy() for n in nodes}

    def poly(n, x, y):
        B = basis(x, y)
        return B @ CU[n], B @ CV[n]

    # stage 2: per-projector correction fields that make every seam agree to
    # measurement accuracy; each seam's residual is split half/half between the
    # two neighbours; the projector's middle column is anchored (no change).
    corr = {}

    def UV(n, x, y):
        u, v = poly(n, x, y)
        if n in corr:
            d = corr[n](np.column_stack([np.ravel(x), np.ravel(y)]))
            u = u + d[:, 0].reshape(np.shape(u)); v = v + d[:, 1].reshape(np.shape(v))
        return u, v

    anchor = np.column_stack([np.full(9, 0.5), np.linspace(0, 1, 9)])
    for _ in range(3):
        pts = {n: [anchor] for n in nodes}; val = {n: [np.zeros((9, 2))] for n in nodes}
        cur = {n: (lambda n=n: None) for n in nodes}
        for (A, B), (pa, pb) in seams.items():
            if A not in idx or B not in idx or len(pa) == 0:
                continue
            ua, va = UV(A, pa[:, 0], pa[:, 1]); ub, vb = UV(B, pb[:, 0], pb[:, 1])
            if ring and A == last and B == first:
                ub = ub + 1
            r = np.column_stack([ua - ub, va - vb])
            if (A, B) in corner_seams:
                r[:, 1] = 0.0          # no vertical bending from unreliable corner data
            pts[A].append(pa); val[A].append(-r / 2)
            pts[B].append(pb); val[B].append(r / 2)
        new_corr = {}
        ex = np.linspace(0, 1, 41)
        for n in nodes:
            P_ = np.concatenate(pts[n]); V_ = np.concatenate(val[n])
            if n in corr:
                V_ = V_ + corr[n](P_)
            # horizontal correction: over the full height
            ru = RBFInterpolator(P_, V_[:, 0], kernel="thin_plate_spline", smoothing=len(P_) * SMOOTH,
                                 neighbors=min(len(P_), 120))
            # vertical correction: pinned to zero along the content's top and bottom
            # edge, so the floor line and the top edge stay straight at the seams
            ta, tb, fa, fb = (borders or {}).get(n, (0.0, 0.0, 0.0, 1.0))
            edge = np.concatenate([np.column_stack([ex, ta * ex + tb]), np.column_stack([ex, fa * ex + fb])])
            PV = np.concatenate([P_, edge]); VV = np.concatenate([V_[:, 1], np.zeros(len(edge))])
            rv = RBFInterpolator(PV, VV, kernel="thin_plate_spline", smoothing=len(PV) * SMOOTH,
                                 neighbors=min(len(PV), 120))
            new_corr[n] = (lambda P, ru=ru, rv=rv: np.column_stack([ru(P), rv(P)]))
        corr.clear(); corr.update(new_corr)

    # seam residuals in content units -> report in fractions of a projector width
    res = {}
    for (A, B), (pa, pb) in seams.items():
        if A in idx and B in idx and len(pa):
            ua, va = UV(A, pa[:, 0], pa[:, 1]); ub, vb = UV(B, pb[:, 0], pb[:, 1])
            if ring and A == last and B == first:
                ub = ub + 1
            res[f"{A}|{B}"] = (ua - ub, va - vb)

    # inner edges per node
    t = np.linspace(0, 1, 41)
    edges = {}
    for n in nodes:
        uL, _ = UV(n, np.zeros_like(t), t); uR, _ = UV(n, np.ones_like(t), t)
        _, vT = UV(n, t, np.zeros_like(t)); _, vB = UV(n, t, np.ones_like(t))
        edges[n] = [float(uL.max()), float(uR.min()), float(vT.max()), float(vB.min())]
    v_top = max(e[2] for e in edges.values()); v_bot = min(e[3] for e in edges.values())
    if borders:
        # measured borders define the content band directly: top edge -> 0, floor -> 1
        v_top, v_bot = 0.0, 1.0
    complete = ring and first in idx and last in idx and (last, first) in seams
    if complete:
        # wrap point: middle of the overlap between the last and the first node
        wrapU = (edges[first][0] + (edges[last][1] - 1.0)) / 2
        span = 1.0
    else:
        wrapU = edges[nodes[0]][0]
        span = edges[nodes[-1]][1] - wrapU
    norm_u = lambda u: (u - wrapU) / span
    norm_v = lambda v: (v - v_top) / (v_bot - v_top)

    # Fade layout for any overlap width: every seam gets one fade zone centred
    # in its overlap; a zone never reaches past 40 % of the way to the next
    # seam, so at most two projectors share any spot and their weights sum to 1.
    inner = {n: (norm_u(edges[n][0]), norm_u(edges[n][1])) for n in nodes}
    chain = [n for n in order if n in idx]
    links = [(chain[i], chain[i + 1]) for i in range(len(chain) - 1) if (chain[i], chain[i + 1]) in seams]
    if complete:
        links.append((last, first))
    cut, half = {}, {}
    for A, B in links:
        if complete and A == last and B == first:
            cut[(A, B)] = (1.0, 0.0); half[(A, B)] = 0.0      # video wrap: hard cut, no blend
            continue
        c = (inner[A][1] + inner[B][0]) / 2
        h_ = max(0.0, (inner[A][1] - inner[B][0]) / 2)
        if (A, B) in corner_seams:
            # wall corner: cut exactly on the measured corner line, narrow fade
            pa_ = seams[(A, B)][0]
            ua_, _ = UV(A, pa_[:, 0], pa_[:, 1])
            c = float(np.median(norm_u(ua_)))
            h_ = min(h_, 0.0015, c - inner[B][0], inner[A][1] - c)
            h_ = max(h_, 0.0)
        cut[(A, B)] = (c, c); half[(A, B)] = h_
    lnk_left = {B: (A, B) for A, B in links}; lnk_right = {A: (A, B) for A, B in links}
    for key in links:
        A, B = key
        c = cut[key][0]
        c_prev = cut[lnk_left[A]][1] if A in lnk_left else inner[A][0]
        c_next = cut[lnk_right[B]][0] if B in lnk_right else inner[B][1]
        if key == (last, first) and complete:
            continue
        half[key] = max(0.0, min(half[key], 0.4 * (c - c_prev), 0.4 * (c_next - cut[key][1])))
    strips, fades = {}, {}
    for n in nodes:
        hl = half[lnk_left[n]] if n in lnk_left else 0.0
        hr = half[lnk_right[n]] if n in lnk_right else 0.0
        lo = cut[lnk_left[n]][1] - hl if n in lnk_left else inner[n][0]
        hi = cut[lnk_right[n]][0] + hr if n in lnk_right else inner[n][1]
        strips[n] = (lo, hi); fades[n] = (hl, hr)

    edits, rep = [], {}
    # forward map sampled a little beyond the picture, so content that reaches the
    # picture's own edge (e.g. the floor at the bottom row) is interpolated, not
    # clamped to the nearest sample (that produced a sudden curve at the edge)
    gxx, gyy = np.meshgrid(np.linspace(-0.15, 1.15, 105), np.linspace(-0.15, 1.15, 79))
    for n in nodes:
        u0, u1 = strips[n]
        Uf, Vf = UV(n, gxx.ravel(), gyy.ravel())
        Uf, Vf = norm_u(Uf), norm_v(Vf)
        su = (Uf - u0) / (u1 - u0)                     # strip-local u
        cols, rws = mesh.get(n, mesh.get("*", (11, 7))) if isinstance(mesh, dict) else mesh
        mu, mv = np.meshgrid(np.linspace(0, 1, cols), np.linspace(0, 1, rws))
        qx = griddata((su, Vf), gxx.ravel(), (mu, mv), method="linear")
        qy = griddata((su, Vf), gyy.ravel(), (mu, mv), method="linear")
        qx = np.where(np.isnan(qx), griddata((su, Vf), gxx.ravel(), (mu, mv), method="nearest"), qx)
        qy = np.where(np.isnan(qy), griddata((su, Vf), gyy.ravel(), (mu, mv), method="nearest"), qy)
        pts = [{"u": round(float(mu[r, c]), 5), "v": round(float(mv[r, c]), 5),
                "x": round(float(qx[r, c] * 2 - 1), 5), "y": round(float(1 - qy[r, c] * 2), 5)}
               for r in range(rws) for c in range(cols)]
        wl = 2 * fades[n][0] / (u1 - u0); wr = 2 * fades[n][1] / (u1 - u0)
        e = lambda wd: {"width": round(min(wd, 0.5), 4), "gamma": round(blend_gamma, 4), "black_lift": 0.0}
        flat = {"width": 0.0, "gamma": round(blend_gamma, 4), "black_lift": 0.0}
        edits += [{"cmd": "set_source_region", "node": n,
                   "source_region": {"x": round(u0, 6), "y": 0.0, "w": round(u1 - u0, 6), "h": 1.0}},
                  {"cmd": "set_mesh", "node": n, "mesh": {"cols": cols, "rows": rws, "points": pts}},
                  {"cmd": "set_blend", "node": n, "blend": {"left": e(wl), "right": e(wr), "top": flat, "bottom": flat}}]
        rep[n] = {"strip": [round(u0, 5), round(u1, 5)], "blend_left": round(wl, 4), "blend_right": round(wr, 4),
                  "clipped_blend": wl > 0.5 or wr > 0.5}
    seam_rep = {}
    for s, (du, dv) in res.items():
        A = s.split("|")[0]
        width = inner[A][1] - inner[A][0] if A in inner else 1
        # residual in output pixels of a 1920-wide projector
        px = np.hypot(du / span / width * 1920, dv / (v_bot - v_top) * 1200)
        seam_rep[s] = {"pairs": int(len(du)), "median_px": round(float(np.median(px)), 2),
                       "p95_px": round(float(np.percentile(px, 95)), 2)}
    report = {"ring": ring, "closed": bool(complete), "nodes": rep, "seams": seam_rep,
              "blend_gamma_convention": "projector-response",
              "missing_seams": [f"{a}|{b}" for a, b in expected_seams(order, ring) if (a, b) not in seams]}
    return edits, report, {"UV": UV, "norm_u": norm_u, "norm_v": norm_v, "strips": strips, "fades": fades}


def solve(order, ring, seams, corner_seams=(), rounds=2, floors=None, borders=None, **kw):
    """Solve, then drop pairs that contradict the result (a corner line placed
    wrongly in one frame, a phantom marker) and solve again."""
    seams = {k: (np.asarray(v[0]), np.asarray(v[1])) for k, v in seams.items()}
    dropped = {}
    for r in range(rounds + 1):
        edits, report, model = _solve_chain(order, ring, seams, corner_seams, floors=floors, borders=borders, **kw)
        if r == rounds:
            break
        UV, nu, nv = model["UV"], model["norm_u"], model["norm_v"]
        new = {}
        for (A, B), (pa, pb) in seams.items():
            key = f"{A}|{B}"
            if key not in report["seams"] or A not in model["strips"] or B not in model["strips"]:
                new[(A, B)] = (pa, pb); continue
            ua, va = UV(A, pa[:, 0], pa[:, 1]); ub, vb = UV(B, pb[:, 0], pb[:, 1])
            du = nu(ua) - nu(ub); du = du - np.round(du)          # wrap seam differs by one turn
            res = np.hypot(du * 1920 * 12, (nv(va) - nv(vb)) * 1200)
            med = np.median(res); mad = np.median(np.abs(res - med)) + 1e-9
            keep = res <= max(med + 3 * 1.4826 * mad, 6.0)
            if keep.sum() >= 10:
                dropped[key] = dropped.get(key, 0) + int((~keep).sum())
                new[(A, B)] = (pa[keep], pb[keep])
            else:
                new[(A, B)] = (pa, pb)
        seams = new
    report["dropped_outliers"] = dropped
    return edits, report, model


def _solve_chain(order, ring, seams, corner_seams=(), floors=None, borders=None, **kw):
    """Solve the longest unbroken chain of measured seams. A ring with a gap is
    solved as an open chain that starts after the gap (no false closure)."""
    order = list(order)
    present = {k for k, v in seams.items() if len(v[0]) >= 10}
    links = expected_seams(order, ring)
    if ring and len(order) > 2 and all(l in present for l in links):
        edits, report, model = _solve_core(order, True, seams, corner_seams=set(corner_seams), floors=floors, borders=borders, **kw)
        report["left_out"] = []
        return edits, report, model
    rot = order
    if ring:
        k = [l in present for l in links].index(False)
        rot = order[k + 1:] + order[:k + 1]
    runs = [[rot[0]]]
    for i in range(1, len(rot)):
        if (rot[i - 1], rot[i]) in present:
            runs[-1].append(rot[i])
        else:
            runs.append([rot[i]])
    best = max(runs, key=len)
    if len(best) < 2:
        raise ValueError("no two neighbouring nodes are linked by a measured seam yet")
    sub = {k: v for k, v in seams.items() if k[0] in best and k[1] in best and best.index(k[1]) == best.index(k[0]) + 1}
    edits, report, model = _solve_core(best, False, sub, corner_seams=set(corner_seams), floors=floors, borders=borders, **kw)
    report["ring"] = ring
    report["missing_seams"] = [f"{a}|{b}" for a, b in links if (a, b) not in present]
    report["left_out"] = [n for n in order if n not in best]
    return edits, report, model


def expected_seams(order, ring):
    s = [(order[i], order[i + 1]) for i in range(len(order) - 1)]
    if ring and len(order) > 2:
        s.append((order[-1], order[0]))
    return s


# ------------------------------------------------------------ self-test ----
def self_test(n=12, shape="square", ring=True, seed=0, ovf=1.6, drop=None):
    """Projectors on the walls of a square (or round) room, heavy overlap, each
    one keystoned and mounted at its own height. Seam pairs come from the true
    geometry plus noise; the result must give identical content in overlaps."""
    rng = np.random.default_rng(seed)
    half = 3.5
    perim = 8 * half if shape == "square" else 2 * math.pi * half
    # projector i covers wall arclength [s0, s0 + width] with 30 % overlap each side
    pitch = perim / n
    width = pitch * ovf
    P = []
    for i in range(n):
        s0 = i * pitch - (width - pitch) / 2 + rng.normal(0, 0.05)
        h0 = 0.4 + rng.normal(0, 0.05); hh = 2.2 + rng.normal(0, 0.05)
        key = rng.normal(0, 0.04)      # keystone: top wider than bottom
        P.append((s0, width, h0, hh, key))

    def to_wall(i, x, y):
        s0, w, h0, hh, key = P[i]
        s = s0 + w * (x + key * (0.5 - y) * (x - 0.5))
        z = h0 + hh * (1 - y)
        return s, z

    def from_wall(i, s, z):
        s0, w, h0, hh, key = P[i]
        y = 1 - (z - h0) / hh
        f = 1 + key * (0.5 - y)
        x = ((s - s0) / w + key * (0.5 - y) * 0.5) / f
        return x, y

    seams, clean = {}, {}
    order = [f"n{i+1}" for i in range(n)]
    for a, b in expected_seams(order, ring):
        i, j = order.index(a), order.index(b)
        pa, pb = [], []
        for _ in range(400):
            x, y = rng.uniform(0, 1, 2)
            s, z = to_wall(i, x, y)
            if j == 0 and i == n - 1:
                s -= perim
            xb, yb = from_wall(j, s, z)
            if 0 <= xb <= 1 and 0 <= yb <= 1:
                pa.append((x, y)); pb.append((xb + rng.normal(0, 0.0007), yb + rng.normal(0, 0.0007)))
                clean.setdefault((a, b), ([], []))
                clean[(a, b)][0].append((x, y)); clean[(a, b)][1].append((xb, yb))
        if (a, b) != drop:
            seams[(a, b)] = (np.array(pa), np.array(pb))
    # measured borders, as the camera delivers them: the floor and the wall top
    # are the same physical height everywhere; in projector rows they differ
    # with each projector's mounting height
    z_floor, z_top = 0.45, 2.55
    borders = {}
    for i, nn in enumerate(order):
        s0, w_, h0, hh, key = P[i]
        y_top = 1 - (z_top - h0) / hh; y_floor = 1 - (z_floor - h0) / hh
        borders[nn] = (0.0, y_top, 0.0, y_floor)
    edits, report, m = solve(order, ring, seams, borders=borders)
    worst = 0.0
    for (a, b), (ca, cb) in clean.items():
        if f"{a}|{b}" not in report["seams"]:
            continue          # seam not measured or not part of the solved chain
        ca, cb = np.array(ca), np.array(cb)
        ua, va = m["UV"](a, ca[:, 0], ca[:, 1]); ub, vb = m["UV"](b, cb[:, 0], cb[:, 1])
        if report["closed"] and a == order[-1] and b == order[0]:
            ub = ub + 1
        su = m["strips"][a][1] - m["strips"][a][0]
        du = (m["norm_u"](ua) - m["norm_u"](ub)) / su * 1920
        dv = (m["norm_v"](va) - m["norm_v"](vb)) * 1200
        worst = max(worst, float(np.percentile(np.hypot(du, dv), 95)))
    # evenness along the wall: content U vs true arclength must be linear
    lin = []
    for i, nn in enumerate(order):
        xs = np.linspace(0.1, 0.9, 9)
        u, _ = m["UV"](nn, xs, np.full_like(xs, 0.5))
        s = np.array([to_wall(i, x, 0.5)[0] for x in xs]) / perim
        uu = m["norm_u"](u); A_ = np.column_stack([s, np.ones_like(s)]); co, *_ = np.linalg.lstsq(A_, uu, rcond=None)
        lin.append(np.ptp(uu - A_ @ co) / (co[0] * width / perim) * 1920)
    # partition of unity: at any wall point inside the content band the linear
    # fade weights of all projectors covering it must add up to 1
    sums = []
    for _ in range(3000):
        sw = rng.uniform(0, perim); z = rng.uniform(0.6, 2.4)
        tot = 0.0; inband = True
        for i, nn in enumerate(order):
            if nn not in m["strips"]:
                continue
            for sh in (0, perim, -perim):
                x, y = from_wall(i, sw + sh, z)
                if not (0 <= x <= 1 and 0 <= y <= 1):
                    continue
                u, v = m["UV"](nn, np.array([x]), np.array([y]))
                U = float(m["norm_u"](u)[0]); V = float(m["norm_v"](v)[0])
                lo, hi = m["strips"][nn]; hl, hr = m["fades"][nn]
                if not (0 <= V <= 1):
                    inband = False
                if lo <= U <= hi:
                    a = (min(1.0, (U - lo) / (2 * hl)) if hl > 0 else 1.0) * (min(1.0, (hi - U) / (2 * hr)) if hr > 0 else 1.0)
                    tot += a
        if inband and tot > 0:
            sums.append(tot)
    sums = np.array(sums)
    covered = report.get("left_out") == [] if not drop else True
    widths = [v["strip"][1] - v["strip"][0] for v in report["nodes"].values()]
    print(f"{shape} ring={ring} overlap x{ovf} drop={drop}: seam p95 {worst:.2f} px, closed={report['closed']}, "
          f"evenness {max(lin):.1f} px, weight sum {sums.min():.3f}..{sums.max():.3f}, "
          f"min strip {min(widths):.3f}, left out {report.get('left_out')}")
    ok = worst < 2.0 and max(lin) < 15 and min(widths) > 0.01
    if report["closed"]:
        ok = ok and abs(sums - 1).max() < 0.02
    else:   # an open chain: only where the two chain ends physically meet may the sum differ
        print(f"   fraction of points with weight sum 1: {np.mean(abs(sums - 1) < 0.02):.3f}")
        ok = ok and np.mean(abs(sums - 1) < 0.02) > 0.85
    return ok


if __name__ == "__main__":
    import sys
    ok = (self_test(12, "square", True) & self_test(12, "round", True) & self_test(7, "square", False)
          & self_test(5, "round", True) & self_test(12, "square", True, ovf=2.6)
          & self_test(12, "square", True, ovf=2.6, drop=("n8", "n9")))
    print("SELF-TEST", "OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)
