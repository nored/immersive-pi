"""markers.py: projected ArUco marker sheets for camera-based room calibration.

One still video (1920x1080, H.264) holds one tile per node. During capture each
node's source_region points at its own tile, so the node shows its own sheet
full screen. Every marker id maps to known projector-pixel corners.

Neighbouring nodes get different colours (R, G, B) so the camera separates
them per colour channel even where the projectors overlap. Works for any node
count, ring or open row.

  python markers.py make --nodes N [--open] --out DIR     # video + layout.json
  python markers.py self-test                              # encode/decode/warp/detect check
"""
from __future__ import annotations

import argparse, json, math, shutil, subprocess, sys
from pathlib import Path

import cv2
import numpy as np

VW, VH = 1920, 1080
PW, PH = 1920, 1200              # projector pixels the sheet is designed for
MCOLS, MROWS = 8, 5              # markers per sheet
DICT = cv2.aruco.DICT_4X4_1000
COLOURS = [(0, 0, 255), (0, 255, 0), (255, 0, 0)]   # BGR: red, green, blue


def grid_for(n):
    for c, r in ((1, 1), (2, 1), (2, 2), (3, 2), (4, 3), (4, 4), (5, 4)):
        if c * r >= n:
            return c, r
    return 5, 4                  # 20 tiles, reused for larger rooms


def colours_for(n, ring):
    col = [i % 3 for i in range(n)]
    if ring and n > 2 and col[-1] == col[0]:
        col[-1] = ({0, 1, 2} - {col[-2], col[0]}).pop()
    return col


def layout(n, ring):
    gc, gr = grid_for(n)
    tiles = gc * gr
    tw, th = VW // gc, VH // gr
    col = colours_for(n, ring)
    nodes = []
    cell_w, cell_h = PW / MCOLS, PH / MROWS
    msize = int(min(cell_w, cell_h) * 0.70)
    for i in range(n):
        t = i % tiles
        x0, y0 = (t % gc) * tw, (t // gc) * th
        markers = {}
        for j in range(MCOLS * MROWS):
            c, r = j % MCOLS, j // MCOLS
            cx, cy = (c + 0.5) * cell_w, (r + 0.5) * cell_h
            h = msize / 2
            mid = t * MCOLS * MROWS + j
            markers[mid] = [[cx - h, cy - h], [cx + h, cy - h], [cx + h, cy + h], [cx - h, cy + h]]
        nodes.append({"index": i, "tile": t, "colour": col[i],
                      "source_region": {"x": x0 / VW, "y": y0 / VH, "w": tw / VW, "h": th / VH},
                      "tile_px": [x0, y0, tw, th], "markers": markers})
    return {"n": n, "ring": ring, "grid": [gc, gr], "projector": [PW, PH], "marker_px": msize,
            "dict": "DICT_4X4_1000", "nodes": nodes}


def render_sheet(node, aruco):
    """The sheet at projector resolution: node colour background, black markers."""
    img = np.zeros((PH, PW, 3), np.uint8)
    img[:] = COLOURS[node["colour"]]
    for mid, corners in node["markers"].items():
        x0, y0 = int(round(corners[0][0])), int(round(corners[0][1]))
        s = int(round(corners[1][0] - corners[0][0]))
        m = cv2.aruco.generateImageMarker(aruco, int(mid), s)
        tile = np.where(m[..., None] > 127, np.array(COLOURS[node["colour"]], np.uint8), 0).astype(np.uint8)
        img[y0:y0 + s, x0:x0 + s] = tile
    return img


def make(n, ring, out: Path, seconds=4):
    out.mkdir(parents=True, exist_ok=True)
    lay = layout(n, ring)
    aruco = cv2.aruco.getPredefinedDictionary(DICT)
    frame = np.zeros((VH, VW, 3), np.uint8)
    for node in lay["nodes"][:lay["grid"][0] * lay["grid"][1]]:
        x0, y0, tw, th = node["tile_px"]
        frame[y0:y0 + th, x0:x0 + tw] = cv2.resize(render_sheet(node, aruco), (tw, th), interpolation=cv2.INTER_AREA)
    png = out / "markers-frame.png"
    cv2.imwrite(str(png), frame)
    name = f"calib-markers-{n}{'r' if ring else 'o'}.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-loop", "1", "-i", str(png),
                    "-t", str(seconds), "-r", "30", "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
                    "-crf", "10", "-tune", "stillimage", "-g", "30", str(out / name)], check=True)
    lay["media"] = name
    (out / "layout.json").write_text(json.dumps(lay))
    return lay, out / name


def detect(img_bgr, lay, aruco=None):
    """Return {node_index: (marker_ids, image corners (k,4,2))}.

    Each colour channel is searched twice: raw (keeps the pattern where two or
    three projectors overlap) and with the other channels subtracted (removes
    a neighbour's pattern where it leaks into this channel). A marker id names
    its tile; the tile names the node (colour breaks ties when tiles repeat)."""
    aruco = aruco or cv2.aruco.getPredefinedDictionary(DICT)
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    params.adaptiveThreshWinSizeMax = 53
    params.errorCorrectionRate = 0.3          # fewer corrected bits: fewer phantom ids
    det = cv2.aruco.ArucoDetector(aruco, params)
    per_tile = MCOLS * MROWS
    tiles = {}
    for nd in lay["nodes"]:
        tiles.setdefault(nd["tile"], []).append(nd)
    b, g, r = cv2.split(img_bgr.astype(np.float32))
    variants = [(0, r), (1, g), (2, b), (0, r - 0.5 * (g + b)), (1, g - 0.5 * (r + b)), (2, b - 0.5 * (r + g))]
    seen = {}
    for colour, ch in variants:
        im = cv2.normalize(np.clip(ch, 0, None), None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        corners, ids, _ = det.detectMarkers(im)
        if ids is None:
            continue
        for c, i in zip(corners, ids.ravel()):
            cands = tiles.get(int(i) // per_tile, [])
            if len(cands) > 1:
                cands = [nd for nd in cands if nd["colour"] == colour]
            if len(cands) != 1:
                continue
            key = (cands[0]["index"], int(i))
            if key not in seen:
                seen[key] = c.reshape(4, 2)
    seen = consistent(seen, lay)
    found = {}
    for (k, i), c in seen.items():
        found.setdefault(k, ([], []))
        found[k][0].append(i); found[k][1].append(c)
    return {k: (np.array(v[0]), np.array(v[1])) for k, v in found.items()}


def consistent(seen, lay):
    """Keep a marker only if a neighbouring marker of the same projector sits
    where this marker's own geometry predicts it (position within 25 % of a
    cell, same orientation). Phantom ids decoded from two superimposed
    patterns have no such neighbour."""
    keep = {}
    per_node = {}
    for (k, i), c in seen.items():
        per_node.setdefault(k, []).append((i, c))
    cell = np.array([PW / MCOLS, PH / MROWS])
    for k, ms in per_node.items():
        nd = lay["nodes"][k]
        proj = {i: np.array(nd["markers"][i], np.float32) for i, _ in ms}
        for i, c in ms:
            # convex, non-degenerate quad
            if not cv2.isContourConvex(c.astype(np.float32).reshape(-1, 1, 2)) or cv2.contourArea(c.astype(np.float32)) < 40:
                continue
            Hm = cv2.getPerspectiveTransform(proj[i], c.astype(np.float32))
            ok = False
            for j, cj in ms:
                if j == i:
                    continue
                d = np.abs(proj[j].mean(0) - proj[i].mean(0)) / cell
                if d.max() > 1.6:                  # only direct neighbours (incl. diagonal)
                    continue
                pred = cv2.perspectiveTransform(proj[j][None], Hm)[0]
                size = np.linalg.norm(c[0] - c[2])
                if np.abs(pred - cj).max() < 0.25 * size * 1.43:
                    ok = True
                    break
            if ok:
                keep[(k, i)] = c
    return keep


def self_test():
    tmp = Path(__file__).with_name("markers-selftest")
    lay, video = make(12, True, tmp, seconds=1)
    cap = cv2.VideoCapture(str(video)); ok, decoded = cap.read()
    assert ok, "could not decode the encoded video"
    # two neighbouring projectors (nodes 3 and 4) on a wall, 25 % overlap,
    # seen by a camera at an angle; light adds in the overlap
    cw, ch = 1280, 720
    wall = np.zeros((ch, cw, 3), np.float32)
    truth = {}
    for k, (node_i, shift) in enumerate(((3, 0), (4, 1))):
        nd = lay["nodes"][node_i]
        x0, y0, tw, th = nd["tile_px"]
        proj = cv2.resize(decoded[y0:y0 + th, x0:x0 + tw], (PW, PH), interpolation=cv2.INTER_LINEAR)
        # projector -> camera homography: projector k spans camera x from 100+shift*500
        src = np.float32([[0, 0], [PW, 0], [PW, PH], [0, PH]])
        left = 60 + shift * 520
        dst = np.float32([[left, 90 + 30 * shift], [left + 700, 60 + 20 * shift],
                          [left + 690, 650 - 10 * shift], [left + 15, 620 + 15 * shift]])
        Hm = cv2.getPerspectiveTransform(src, dst)
        wall += cv2.warpPerspective(proj, Hm, (cw, ch)).astype(np.float32)
        truth[node_i] = Hm
    cam = np.clip(wall * 0.8 + 12 + np.random.default_rng(0).normal(0, 3, wall.shape), 0, 255).astype(np.uint8)
    cam = cv2.GaussianBlur(cam, (3, 3), 0.8)
    cv2.imwrite(str(tmp / "selftest-camera.jpg"), cam)
    found = detect(cam, lay)
    worst = 0.0
    report = {}
    for node_i, Hm in truth.items():
        ids, cs = found.get(node_i, (np.array([]), np.zeros((0, 4, 2))))
        nd = lay["nodes"][node_i]
        errs = []
        for mid, c in zip(ids, cs):
            pc = np.float32(nd["markers"][int(mid)]).reshape(-1, 1, 2)
            exp = cv2.perspectiveTransform(pc, Hm).reshape(4, 2)
            errs.append(np.abs(exp - c).max())
        e = float(np.median(errs)) if errs else float("nan")
        report[node_i] = {"markers": int(len(ids)), "median_corner_err_px": round(e, 3)}
        worst = max(worst, e if errs else 99)
    print(json.dumps({"grid": lay["grid"], "marker_px": lay["marker_px"], "detected": report}))
    ok = all(v["markers"] >= 25 for v in report.values()) and worst < 1.0
    print("SELF-TEST", "OK" if ok else "FAILED")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["make", "self-test"])
    ap.add_argument("--nodes", type=int); ap.add_argument("--open", action="store_true")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.cmd == "self-test":
        sys.exit(0 if self_test() else 1)
    lay, video = make(a.nodes, not a.open, a.out)
    print(video, lay["grid"], "marker", lay["marker_px"], "px")
