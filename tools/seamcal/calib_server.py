"""calib_server.py: handheld camera calibration of n fixed projectors.

Run on a computer with a camera and a browser, open http://localhost:8099.
The browser streams camera frames here; projected ArUco markers in every frame
tie each projector to its neighbours; the solver turns that into crop, warp
mesh and blend for every node. All changes go through the controller's
WebSocket (same commands as the calibration website) and its media upload.

  CTL_HOST=<controller host> python calib_server.py
"""
from __future__ import annotations

import json, os, pickle, shutil, subprocess, tempfile, threading, time, traceback, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np
from websockets.sync.client import connect

import corner
import floorline
import markers
import solve_wall
from quality import validate_candidate

PORT = int(os.environ.get("PORT", "8099"))
CTL_HOST = os.environ.get("CTL_HOST", "dortmund-control.local")
HTTP_PORT = int(os.environ.get("CTL_HTTP_PORT", "8080"))
WORK = Path(os.environ.get("WORK", str(Path(__file__).with_name("calib-work")))); WORK.mkdir(exist_ok=True)
BINS_X, BINS_Y = 40, 25
SEAM_ROWS_NEEDED = 0.8
SEAM_FRAMES_NEEDED = 8


# --------------------------------------------------------------- control ----
def ctl_session():
    ws = connect(f"ws://{CTL_HOST}:8765", max_size=None, open_timeout=5)
    ws.send(json.dumps({"role": "web"}))
    snap = json.loads(ws.recv())
    alive = [True]

    def drain():
        try:
            while alive[0]:
                ws.recv()
        except Exception:
            pass
    threading.Thread(target=drain, daemon=True).start()

    def close():
        alive[0] = False
        try:
            ws.close()
        except Exception:
            pass
    return ws, snap, close


def send_all(msgs):
    ws, snap, close = ctl_session()
    try:
        for m in msgs:
            ws.send(json.dumps(m))
            time.sleep(0.01)
        time.sleep(0.5)
    finally:
        close()
    return snap


def upload(path: Path):
    req = urllib.request.Request(f"http://{CTL_HOST}:{HTTP_PORT}/api/upload?name={path.name}",
                                 data=path.read_bytes(), method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# ----------------------------------------------------------------- state ----
S = {"mode": "idle", "msg": "Press 'Show markers' to start.", "ring": True, "order": [], "frames": 0,
     "last": {}, "report": None, "busy": False}
LOCK = threading.Lock()
SEAMS = {}       # (A, B) -> {"bins": {bin: [[pa...], [pb...], set(frames)]}}
LAY = {}
SHEETS = {}
XR = {}        # node index -> (lo, hi) columns on its own wall
CORNER = {}     # seam -> number of frames measured with the corner method
FLOOR = {}      # node -> list of (slope, offset): floor line y = slope*x + offset in projector coords
TOP = {}        # node -> list of (slope, offset): upper border (wall top fold or picture top)
WALLTOP = {}    # node -> list of (slope, offset): the wall's upper end above the picture


def order_from_room(room):
    nodes = room["nodes"]
    return [e["node"] for e in sorted(nodes, key=lambda e: (e.get("source_region", {}).get("x", 0), e["node"]))]


def neighbours_pairs(order, ring):
    return solve_wall.expected_seams(order, ring)


def start_markers(ring):
    ws, snap, close = ctl_session(); close()
    room, show = snap["room"], snap["show"]
    backup = WORK / "backup.json"
    if not backup.exists():   # keep the very first backup until it is restored or applied
        backup.write_text(json.dumps({"room": room, "show": show, "time": time.strftime("%Y-%m-%d %H:%M:%S")}))
    room = json.loads(backup.read_text())["room"]
    order = order_from_room(room)
    lay, video = markers.make(len(order), ring, WORK / "markers")
    lay["order"] = order
    upload(video)
    send_markers(lay, order)
    LAY.clear(); LAY.update(lay)
    SHEETS.clear(); SHEETS.update(corner.load_sheets(lay, WORK / "markers" / "markers-frame.png"))
    with LOCK:
        SEAMS.clear(); CORNER.clear(); FLOOR.clear(); TOP.clear(); WALLTOP.clear(); S["frames"] = 0
    (WORK / "seams.pkl").unlink(missing_ok=True)
    S.update(mode="markers", ring=ring, order=order,
             msg=f"Markers are on all {len(order)} projectors.")


def send_markers(lay, order):
    msgs = []
    for nd, name in zip(lay["nodes"], order):
        msgs += [{"cmd": "scan", "node": name, "on": False},
                 {"cmd": "testpattern", "node": name, "on": False},
                 {"cmd": "blackout", "node": name, "on": False},
                 {"cmd": "set_source_region", "node": name, "source_region": nd["source_region"]},
                 {"cmd": "set_mesh", "node": name, "mesh": {"cols": 2, "rows": 2, "points": [
                     {"u": 0, "v": 0, "x": -1, "y": 1}, {"u": 1, "v": 0, "x": 1, "y": 1},
                     {"u": 0, "v": 1, "x": -1, "y": -1}, {"u": 1, "v": 1, "x": 1, "y": -1}]}},
                 {"cmd": "set_blend", "node": name, "blend": {e: {"width": 0, "gamma": 1, "black_lift": 0}
                                                              for e in ("left", "right", "top", "bottom")}}]
    msgs.append({"cmd": "play_media", "media": lay["media"]})
    send_all(msgs)


def try_edits():
    """Send the solved calibration live and play the previous video. Nothing is
    saved: the controller's room file, the backup and the captured data stay."""
    b = json.loads((WORK / "backup.json").read_text())
    edits = json.loads((WORK / "edits.json").read_text())
    validate_candidate(edits, json.loads((WORK / "report.json").read_text()), S["order"])
    msgs = [{"cmd": "set_color", "node": e["node"], "color": e["color"]} for e in b["room"]["nodes"]]
    msgs += edits
    media = b["show"].get("media")
    if media:
        msgs.append({"cmd": "play_media", "media": media})
    send_all(msgs)
    S.update(mode="preview", msg="Trying the calibration (not saved). Back = markers again, Apply = keep it.")


def back_to_markers():
    send_markers(LAY, LAY["order"])
    S.update(mode="markers", msg="Markers are back; captured data kept.")


def restore(apply_edits=None, message="restore room after calibration capture"):
    b = json.loads((WORK / "backup.json").read_text())
    msgs = []
    for e in b["room"]["nodes"]:
        n = e["node"]
        msgs += [{"cmd": "set_source_region", "node": n, "source_region": e["source_region"]},
                 {"cmd": "set_mesh", "node": n, "mesh": e["mesh"]},
                 {"cmd": "set_blend", "node": n, "blend": e["blend"]},
                 {"cmd": "set_color", "node": n, "color": e["color"]}]
    if apply_edits:
        msgs += apply_edits
    media = b["show"].get("media")
    if media:
        msgs.append({"cmd": "play_media", "media": media})
    msgs.append({"cmd": "save", "message": message})
    send_all(msgs)
    (WORK / "backup.json").rename(WORK / f"backup-used-{int(time.time())}.json")
    S.update(mode="idle")
    with LOCK:
        SEAMS.clear(); S["frames"] = 0
    (WORK / "seams.pkl").unlink(missing_ok=True)


# ------------------------------------------------------------- per frame ----
CELL = 1.0 / 0.70     # marker cell / marker size (markers.layout uses 70 %)


def marker_maps(node, ids, cs, shape):
    """Per-marker local homographies (camera -> normalised projector) and a
    label image telling which marker's cell covers each camera pixel. Local to
    one marker cell, so wall corners, doors and curved walls do not matter."""
    nd = LAY["nodes"][LAY["order"].index(node)]
    label = np.zeros(shape, np.int32)
    Hs = [None]
    for mid, c in zip(ids, cs):
        pc = np.array(nd["markers"][int(mid)], np.float64) / [markers.PW, markers.PH]
        Hm = cv2.getPerspectiveTransform(c.astype(np.float32), pc.astype(np.float32))
        ctr = pc.mean(0)
        cell = (pc - ctr) * CELL + ctr                      # the marker's whole cell, in projector space
        quad = cv2.perspectiveTransform(cell[None].astype(np.float32), np.linalg.inv(Hm))[0]
        Hs.append(Hm)
        cv2.fillConvexPoly(label, np.round(quad).astype(np.int32), len(Hs) - 1)
    return label, Hs


def process(img, step=6, corners_only=False, keep=True, floor_only=False):
    lay = LAY
    order = lay["order"]
    if max(img.shape[:2]) > 4100:
        f = 4100 / max(img.shape[:2]); img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    found = markers.detect(img, lay)
    det = {order[k]: (ids, cs) for k, (ids, cs) in found.items()}
    with LOCK:
        S["frames"] += 1
        fid = S["frames"]
    if keep:
        keep_frame(img, det, fid)
    h, w = img.shape[:2]
    if not corners_only:
        for k, v in floorline.borders(img, lay, found, SHEETS, xrange=XR).items():
            for kind, store in (("floor", FLOOR), ("top", TOP), ("walltop", WALLTOP)):
                if v.get(kind) is not None:
                    a_, b_ = np.polyfit(v[kind][:, 0], v[kind][:, 1], 1)
                    with LOCK:
                        store.setdefault(order[k], []).append((float(a_), float(b_)))
    if floor_only:
        return {}
    gy, gx = np.mgrid[step // 2:h:step, step // 2:w:step]
    gy, gx = gy.ravel(), gx.ravel()
    maps = {}
    seams_seen = []
    for A, B in neighbours_pairs(order, S["ring"]):
        if A not in det or B not in det or len(det[A][0]) < 2 or len(det[B][0]) < 2:
            continue
        for n in (A, B):
            if n not in maps:
                maps[n] = marker_maps(n, det[n][0], det[n][1], (h, w))
        la, Ha = maps[A]; lb, Hb = maps[B]
        a = la[gy, gx]; b = lb[gy, gx]
        both = (a > 0) & (b > 0)
        if corners_only or both.sum() < 300:
            # few or no readable markers inside the overlap: the seam may sit on a
            # wall corner -> measure along the corner line instead
            if SHEETS and len(det[A][0]) >= 6 and len(det[B][0]) >= 6:
                r = corner.corner_pairs(img, lay, SHEETS, order.index(A), order.index(B), found)
                if r is not None:
                    add_pairs(A, B, r[0], r[1], fid)
                    with LOCK:
                        CORNER[(A, B)] = CORNER.get((A, B), 0) + 1
                    seams_seen.append(f"{A}|{B}")
            if corners_only or both.sum() < 20:
                continue
        cam = np.column_stack([gx[both], gy[both]]).astype(np.float32)
        a, b = a[both], b[both]
        pa = np.zeros((len(cam), 2), np.float32); pb = np.zeros_like(pa)
        for k in np.unique(a):
            sel = a == k; pa[sel] = cv2.perspectiveTransform(cam[sel][None], Ha[k])[0]
        for k in np.unique(b):
            sel = b == k; pb[sel] = cv2.perspectiveTransform(cam[sel][None], Hb[k])[0]
        ok = np.all((pa >= 0) & (pa <= 1), 1) & np.all((pb >= 0) & (pb <= 1), 1)
        pa, pb = pa[ok], pb[ok]
        if len(pa) < 20:
            continue
        add_pairs(A, B, pa, pb, fid)
        if f"{A}|{B}" not in seams_seen:
            seams_seen.append(f"{A}|{B}")
    if fid % 20 == 0:
        save_seams()
    S["last"] = {"nodes": {n: int(len(det[n][0])) for n in det}, "seams": seams_seen,
                 "boxes": {n: det[n][1].reshape(-1, 2).mean(0).tolist() for n in det},
                 "quads": {n: np.round(det[n][1]).astype(int).tolist() for n in det}, "size": [w, h]}
    return S["last"]


def process_video(path: Path, fps=4.0):
    """Frames of a phone video (any lens, up to 4K) -> process() each one."""
    tmp = Path(tempfile.mkdtemp(prefix="calibvid-", dir=WORK))
    try:
        S["msg"] = f"Extracting frames from {path.name} ..."
        subprocess.run([shutil.which("ffmpeg"), "-loglevel", "error", "-i", str(path), "-vf",
                        f"fps={fps},scale='min(3840,iw)':-2", "-q:v", "2", str(tmp / "f%05d.jpg")], check=True)
        frames = sorted(tmp.glob("f*.jpg"))
        hits = {}
        for k, f in enumerate(frames):
            r = process(cv2.imread(str(f)))
            for sname in r["seams"]:
                hits[sname] = hits.get(sname, 0) + 1
            S["msg"] = f"Video {path.name}: frame {k + 1}/{len(frames)}"
        save_seams()
        S["msg"] = (f"Video {path.name}: {len(frames)} frames, seams seen: "
                    + (", ".join(f"{a.split('-')[-1]}|{b.split('-')[-1]} x{n}" for (a, b), n in
                                 ((tuple(k.split('|')), v) for k, v in hits.items())) or "none"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        path.unlink(missing_ok=True)


def keep_frame(img, det, fid, limit=300):
    """Last `limit` camera frames + what was detected, for diagnosis."""
    d = WORK / "frames"; d.mkdir(exist_ok=True)
    name = f"{time.time():.3f}-{fid:06d}"            # time first: newest sorts last
    cv2.imwrite(str(d / f"{name}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    (d / f"{name}.json").write_text(json.dumps({n: [int(x) for x in v[0]] for n, v in det.items()}))
    old = sorted(d.glob("*.jpg"))[:-limit]
    for f in old:
        f.unlink(missing_ok=True); f.with_suffix(".json").unlink(missing_ok=True)


def add_pairs(A, B, pa, pb, fid):
    with LOCK:
        seam = SEAMS.setdefault((A, B), {})
        bx = np.clip((pa[:, 0] * BINS_X).astype(int), 0, BINS_X - 1)
        by = np.clip((pa[:, 1] * BINS_Y).astype(int), 0, BINS_Y - 1)
        key = by * BINS_X + bx
        for k in np.unique(key):
            sel = key == k
            cell = seam.setdefault(int(k), [[], [], set()])
            if len(cell[0]) < 600:
                cell[0].extend(pa[sel][:60].tolist()); cell[1].extend(pb[sel][:60].tolist())
            cell[2].add(fid)


def own_wall_xrange():
    """Per node index: the column range on its own wall, from the corner seams."""
    order = LAY["order"]
    rng = {k: [0.0, 1.0] for k in range(len(order))}
    for (A, B), n in CORNER.items():
        if n < 4 or (A, B) not in SEAMS:
            continue
        pa, pb = [], []
        for c in SEAMS[(A, B)].values():
            if len(c[2]) >= 3:
                pa.append(np.median(np.array(c[0]), 0)); pb.append(np.median(np.array(c[1]), 0))
        if len(pa) < 5:
            continue
        ka, kb = order.index(A), order.index(B)
        rng[ka][1] = min(rng[ka][1], float(np.percentile(np.array(pa)[:, 0], 10)))   # A's own wall ends at the corner
        rng[kb][0] = max(rng[kb][0], float(np.percentile(np.array(pb)[:, 0], 90)))   # B's own wall starts at the corner
    return {k: tuple(v) for k, v in rng.items()}


def reprocess_floor():
    global XR
    XR = own_wall_xrange()
    with LOCK:
        FLOOR.clear()
    files = sorted((WORK / "frames").glob("*.jpg")) + sorted((WORK / "frames-old").glob("*.jpg"))
    for k, f in enumerate(files):
        img = cv2.imread(str(f))
        found = markers.detect(img, LAY)
        for kk, v in floorline.borders(img, LAY, found, SHEETS, xrange=XR).items():
            if v["floor"] is not None:
                a_, b_ = np.polyfit(v["floor"][:, 0], v["floor"][:, 1], 1)
                with LOCK:
                    FLOOR.setdefault(LAY["order"][kk], []).append((float(a_), float(b_)))
        S["msg"] = f"floor: frame {k + 1}/{len(files)}"
    save_seams()


def border_lines():
    """Equalized content band for all projectors. Each projector gets a real
    height scale from its floor line (measured; else its wall's median, else the
    overall median) to its wall top (same fallbacks). The common band runs from
    the floor to the lowest top row of all projectors: below the floor and above
    the band a projector stays black. Returns {node: (top_a, top_b, floor_a, floor_b)}."""
    f, wt = robust_lines(FLOOR, 3), robust_lines(WALLTOP, 5)
    order = S["order"]
    corners = {k for k, n in CORNER.items() if n >= 4}
    walls, cur = [], [order[0]]
    for i in range(1, len(order)):
        if (order[i - 1], order[i]) in corners:
            walls.append(cur); cur = []
        cur.append(order[i])
    walls.append(cur)
    if S["ring"] and len(walls) > 1 and (order[-1], order[0]) not in corners:
        walls[0] = walls.pop() + walls[0]
    wall_of = {n: i for i, w in enumerate(walls) for n in w}

    def mid(line):
        return line[0] * 0.5 + line[1]

    fl = {n: f[n] for n in order if n in f}                   # (slope, offset) of the floor line, measured
    # a slant only counts when it is consistent: >= 8 frames, spread < 20 rows;
    # otherwise the wall's reliable median slant (or level) is used
    reliable = {}
    for n in fl:
        v = np.array(FLOOR[n])
        if len(v) >= 8 and np.std(v[:, 0]) * 800 < 20:
            reliable[n] = fl[n][0]
    for n in list(fl):
        if n not in reliable:
            w = walls[wall_of[n]]
            ws = [reliable[m] for m in w if m in reliable]
            sl = float(np.median(ws)) if ws else 0.0
            fl[n] = (sl, mid(fl[n]) - 0.5 * sl)
    tp = {n: mid(wt[n]) for n in order if n in wt}
    allf = np.array(list(fl.values())) if fl else np.array([[0.0, 1.0]])
    g_fl = (float(np.median(allf[:, 0])), float(np.median(allf[:, 1])))
    g_span = float(np.median([mid(fl.get(n, g_fl)) - tp[n] for n in tp])) if tp else None
    if g_span is None:
        return {}
    floor_l, span = {}, {}
    for n in order:
        w = walls[wall_of[n]]
        wf = np.array([fl[m] for m in w if m in fl])
        floor_l[n] = fl[n] if n in fl else ((float(np.median(wf[:, 0])), float(np.median(wf[:, 1]))) if len(wf) else g_fl)
        ws = [mid(fl.get(m, floor_l[n])) - tp[m] for m in w if m in tp]
        span[n] = float(np.median(ws)) if ws else g_span          # wall height in this projector's rows
    # top row height above the floor as a fraction of the wall height, checked at
    # both picture ends (the floor line is slanted in most projectors' grids)
    level = min(min(floor_l[n][1], floor_l[n][0] + floor_l[n][1]) / span[n] for n in order)
    out = {}
    for n in order:
        fa, fb = floor_l[n]
        out[n] = (float(fa), float(fb - level * span[n]), float(fa), float(fb))   # top edge parallel to the floor line
    S["level"] = {"wall_fraction": round(level, 4), "walls": [[m[15:] for m in w] for w in walls],
                  "floor_measured": [n[15:] for n in order if n in fl], "walltop_measured": [n[15:] for n in order if n in tp],
                  "floor_slant_rows": {n[15:]: round(800 * floor_l[n][0]) for n in order},
                  "rows_black_top_mid": {n[15:]: round(800 * (mid(floor_l[n]) - level * span[n])) for n in order},
                  "rows_black_bottom_mid": {n[15:]: round(800 * (1 - mid(floor_l[n]))) for n in order}}
    return out


def robust_lines(store, need=5):
    out = {}
    for n, v in store.items():
        v = np.array(v)
        if len(v) < need:
            continue
        mid = v[:, 1] + 0.5 * v[:, 0]
        m = np.median(mid); keep = np.abs(mid - m) < max(0.05, 2.5 * np.median(np.abs(mid - m)))
        if keep.sum() >= need:
            out[n] = (float(np.median(v[keep, 0])), float(np.median(v[keep, 1])))
    return out


def reprocess_corners():
    """Run the corner method over the saved frames (marker pairs are not added again)."""
    files = sorted((WORK / "frames").glob("*.jpg"))
    for k, f in enumerate(files):
        process(cv2.imread(str(f)), corners_only=True, keep=False)
        S["msg"] = f"corners: frame {k + 1}/{len(files)}"
    save_seams()
    S["msg"] = "corners: " + (", ".join(f"{a.split('-')[-1]}|{b.split('-')[-1]} in {n} frames" for (a, b), n in CORNER.items()) or "none found")


def save_seams():
    with LOCK:
        blob = pickle.dumps({"order": S["order"], "ring": S["ring"], "frames": S["frames"], "seams": SEAMS, "corner": CORNER, "floor": FLOOR, "top": TOP, "walltop": WALLTOP})
    tmp = WORK / "seams.pkl.tmp"; tmp.write_bytes(blob); tmp.replace(WORK / "seams.pkl")


def resume():
    """After a restart: markers still on the walls (backup exists) -> marker mode."""
    if not (WORK / "backup.json").exists() or not (WORK / "markers" / "layout.json").exists():
        return
    lay = json.loads((WORK / "markers" / "layout.json").read_text())
    lay["nodes"] = [dict(nd, markers={int(k): v for k, v in nd["markers"].items()}) for nd in lay["nodes"]]
    room = json.loads((WORK / "backup.json").read_text())["room"]
    lay["order"] = order_from_room(room)
    LAY.clear(); LAY.update(lay)
    SHEETS.clear(); SHEETS.update(corner.load_sheets(lay, WORK / "markers" / "markers-frame.png"))
    S.update(mode="markers", ring=lay["ring"], order=lay["order"],
             msg="Resumed: markers are on the walls. Continue capturing.")
    pk = WORK / "seams.pkl"
    if pk.exists():
        d = pickle.loads(pk.read_bytes())
        if d["order"] == lay["order"] and d["ring"] == lay["ring"]:
            SEAMS.update(d["seams"]); S["frames"] = d["frames"]; CORNER.update(d.get("corner", {})); FLOOR.update(d.get("floor", {})); TOP.update(d.get("top", {})); WALLTOP.update(d.get("walltop", {}))
    if (WORK / "report.json").exists() and (WORK / "edits.json").exists():
        S["report"] = json.loads((WORK / "report.json").read_text())


def seam_status():
    out = []
    for A, B in neighbours_pairs(S["order"], S["ring"]) if S["order"] else []:
        seam = SEAMS.get((A, B), {})
        good = {k for k, c in seam.items() if len(c[2]) >= 3}
        rows = len({k // BINS_X for k in good}) / BINS_Y
        frames = len(set().union(*[c[2] for c in seam.values()])) if seam else 0
        out.append({"seam": f"{A}|{B}", "rows": round(rows, 2), "bins": len(good), "frames": frames,
                    "done": rows >= SEAM_ROWS_NEEDED and frames >= SEAM_FRAMES_NEEDED})
    return out


def seam_pairs():
    pairs = {}
    with LOCK:
        for key, seam in SEAMS.items():
            pa, pb = [], []
            for c in seam.values():
                if len(c[2]) >= 3:
                    pa.append(np.median(np.array(c[0]), 0)); pb.append(np.median(np.array(c[1]), 0))
            if len(pa) >= 10:
                pairs[key] = (np.array(pa), np.array(pb))
    return pairs


def run_solve():
    global XR
    XR = own_wall_xrange()
    save_seams()
    pairs = seam_pairs()
    frames_of = {k: len(set().union(*[c[2] for c in v.values()])) for k, v in SEAMS.items() if v}
    corners = {k for k, n in CORNER.items() if k in frames_of and (n >= 4 or n >= 0.3 * frames_of[k])}
    # vertical from the measured borders: floor line and a level top edge derived
    # from each projector's measured wall top
    bl = border_lines()
    # mesh size: MESH for all projectors, CORNER_MESH for projectors next to a corner
    m_all = tuple(int(v) for v in os.environ.get("MESH", "2x2").split("x"))
    m_cor = tuple(int(v) for v in os.environ.get("CORNER_MESH", os.environ.get("MESH", "2x2")).split("x"))
    mesh = {"*": m_all}
    for a, b in corners:
        mesh[a] = m_cor; mesh[b] = m_cor
    edits, report, model = solve_wall.solve(S["order"], S["ring"], pairs, corner_seams=corners, borders=bl, mesh=mesh)
    report["mesh"] = {"all": "x".join(map(str, m_all)), "corner_projectors": "x".join(map(str, m_cor))}
    report["borders"] = {n: [round(x, 4) for x in v] for n, v in bl.items()}
    report["level"] = S.get("level")
    report["corner_seams"] = [f"{a}|{b}" for a, b in corners]
    (WORK / "edits.json").write_text(json.dumps(edits))
    (WORK / "report.json").write_text(json.dumps(report, indent=1))
    S["report"] = report
    return report


# ------------------------------------------------------------------ page ----
PAGE = (Path(__file__).with_name("page.html")).read_text()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            b = PAGE.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif u.path == "/api/status":
            fl = {n: len(v) for n, v in FLOOR.items()}; tp = {n: len(v) for n, v in WALLTOP.items()}
            self._json({k: S[k] for k in ("mode", "msg", "ring", "order", "frames", "report", "busy")}
                       | {"seams": seam_status(), "floor": {n: fl.get(n, 0) for n in S["order"]},
                          "top": {n: tp.get(n, 0) for n in S["order"]}})
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path); qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            if u.path == "/api/frame":
                if S["mode"] != "markers":
                    return self._json({"error": "markers are not on"})
                img = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR) if body else None
                if img is None:                     # empty / broken frame (camera paused): skip it
                    return self._json({"nodes": {}, "seams": [], "boxes": {}, "quads": {}, "size": [0, 0]})
                return self._json(process(img))
            if u.path == "/api/video":
                if S["mode"] != "markers":
                    return self._json({"error": "markers are not on"})
                name = Path(qs.get("name", "video.mov")).name
                dst = WORK / f"upload-{int(time.time())}-{name}"
                dst.write_bytes(body)

                def job():
                    S["busy"] = True
                    try:
                        process_video(dst)
                    except Exception as e:
                        traceback.print_exc(); S["msg"] = f"Video failed: {e}"
                    finally:
                        S["busy"] = False
                threading.Thread(target=job, daemon=True).start()
                return self._json({"ok": True})
            if S["busy"]:
                return self._json({"error": "busy"})
            S["busy"] = True
            try:
                if u.path == "/api/start":
                    start_markers(qs.get("ring", "1") == "1")
                elif u.path == "/api/try":
                    try_edits()
                elif u.path == "/api/back":
                    back_to_markers()
                elif u.path == "/api/floor":
                    reprocess_floor()
                elif u.path == "/api/corners":
                    reprocess_corners()
                elif u.path == "/api/solve":
                    rep = run_solve()
                    bad = [s for s, v in rep["seams"].items() if v["p95_px"] > 4]
                    S["msg"] = ("Solved. " + (f"Missing seams: {', '.join(rep['missing_seams'])}. " if rep["missing_seams"] else "")
                                + (f"Check seams {', '.join(bad)} (residual above 4 px). " if bad else "All seams agree. ")
                                + "Press 'Apply to room' to write it, or keep capturing.")
                elif u.path == "/api/apply":
                    edits = json.loads((WORK / "edits.json").read_text())
                    validate_candidate(edits, json.loads((WORK / "report.json").read_text()), S["order"])
                    restore(apply_edits=edits, message="camera calibration (markers, wall solve)")
                    S.update(msg="Calibration written and saved. The previous video is playing again.", report=None)
                elif u.path == "/api/restore":
                    if (WORK / "backup.json").exists():
                        restore()
                    S.update(mode="idle", msg="Room restored to its state before the capture.")
                else:
                    return self._json({"error": "unknown"}, 404)
            finally:
                S["busy"] = False
            return self._json({"ok": True})
        except Exception as e:
            traceback.print_exc()
            S["msg"] = f"FAILED: {e}"
            return self._json({"error": str(e)})


def serve_https():
    """Same page over HTTPS on the LAN, so a phone's browser may use its own
    cameras (browsers allow the camera only on https or localhost)."""
    import ssl
    cert, key = Path(__file__).with_name("tls-cert.pem"), Path(__file__).with_name("tls-key.pem")
    if not cert.exists():
        return
    srv = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("HTTPS_PORT", "8443"))), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("phone page on https port " + os.environ.get("HTTPS_PORT", "8443"))


if __name__ == "__main__":
    resume()
    serve_https()
    print(f"open http://localhost:{PORT}  (controller {CTL_HOST})")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
