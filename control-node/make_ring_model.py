#!/usr/bin/env python3
"""make_ring_model.py — generate an N-node ring room-model.

Lays N projectors around a 360° ring driven by one flat panoramic source. Each
node shares an explicit fraction of its output width with each neighbour,
with matching source coverage and left/right soft-edge blends. The mesh starts
as a 2x2 corner-pin (identity) per node — the general primitive at its lowest
resolution — because real geometry comes from the in-room calibration tool, not
from a guess here.

The result is a starting point you load, then calibrate on the wall. It is NOT a
baked mapping: every value is editable and git-versioned from the control node.

    python3 make_ring_model.py --nodes 12 --overlap 0.08 --out room-model.json

Here 0.08 means 8% of each projector's output width. Older versions mixed
panorama and output units; regenerate a draft rather than reusing those units.
This command only writes the requested file. Existing room calibration stays
unchanged. Tiled videos must include the generated overlapping source regions;
disjoint per-node clips cannot supply their neighbours' pixels.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def edge(width=0.0, gamma=2.2, black_lift=0.0):
    return {"width": width, "gamma": gamma, "black_lift": black_lift}


def corner_pin_mesh():
    return {
        "cols": 2, "rows": 2,
        "points": [
            {"u": 0.0, "v": 0.0, "x": -1.0, "y": 1.0},
            {"u": 1.0, "v": 0.0, "x": 1.0, "y": 1.0},
            {"u": 0.0, "v": 1.0, "x": -1.0, "y": -1.0},
            {"u": 1.0, "v": 1.0, "x": 1.0, "y": -1.0},
        ],
    }


def build(nodes: int, overlap: float, ring: bool, black_lift: float):
    if type(nodes) is not int or nodes < 1:
        raise ValueError('nodes must be a positive integer')
    if not math.isfinite(overlap) or not 0 <= overlap < 0.5:
        raise ValueError('overlap must be a projector-width fraction from 0 up to 0.5 (exclusive)')
    if not math.isfinite(black_lift) or not 0 <= black_lift <= 1:
        raise ValueError('black lift must be between 0 and 1')
    # Output blend widths are fractions of ONE projector, not the panorama.
    # Adjacent source windows therefore intersect by overlap * src_w.
    overlap = overlap if nodes > 1 else 0.0
    joins = nodes if ring and nodes > 1 else nodes - 1
    src_w = 1.0 / (nodes - joins * overlap)
    step = src_w * (1 - overlap)

    entries = []
    for i in range(nodes):
        name = f"pi-{i+1:02d}"
        x = i * step
        has_left = nodes > 1 and (ring or i > 0)
        has_right = nodes > 1 and (ring or i < nodes - 1)
        entries.append({
            "node": name,
            "projector": f"P{i+1}",
            "source_region": {"x": x, "y": 0.0, "w": src_w,
                              "h": 1.0, "wrap": bool(ring and nodes > 1)},
            "mesh": corner_pin_mesh(),
            "blend": {
                "left": edge(overlap if has_left else 0.0, 2.2, black_lift if has_left else 0.0),
                "right": edge(overlap if has_right else 0.0, 2.2, black_lift if has_right else 0.0),
                "top": edge(),
                "bottom": edge(),
            },
            "color": {"gain": [1.0, 1.0, 1.0], "gamma": 1.0, "lift": [0.0, 0.0, 0.0]},
        })

    return {
        "version": 1,
        "show": "ambient-loop",
        "media": "pan.mp4",
        "loop": True,
        "ring": ring,
        "canvas": {"cols": nodes, "rows": 1},
        "nodes": entries,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=12)
    ap.add_argument("--overlap", type=float, default=0.08,
                    help="fraction of each projector width shared with each neighbour (0 to <0.5); formerly used inconsistent panorama units")
    ap.add_argument("--black-lift", type=float, default=0.0)
    ap.add_argument("--strip", action="store_true",
                    help="open strip instead of a closed 360 ring")
    ap.add_argument("--out", type=Path, default=Path("room-model.ring.json"))
    args = ap.parse_args(argv)

    try:
        model = build(args.nodes, args.overlap, ring=not args.strip,
                      black_lift=args.black_lift)
    except ValueError as exc:
        ap.error(str(exc))
    args.out.write_text(json.dumps(model, indent=2) + "\n")
    print(f"wrote {args.out} — {args.nodes} nodes, "
          f"{'ring' if not args.strip else 'strip'}, overlap {args.overlap}")
    print("load it as the control node's room-model, then calibrate on the wall.")


if __name__ == "__main__":
    main()
