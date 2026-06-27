#!/usr/bin/env python3
"""make_ring_model.py — generate an N-node ring room-model.

Lays N projectors around a 360° ring driven by one flat panoramic source. Each
node owns an equal horizontal slice of the source plus an overlap margin into
its right neighbour, with matching left/right soft-edge blends. The mesh starts
as a 2x2 corner-pin (identity) per node — the general primitive at its lowest
resolution — because real geometry comes from the in-room calibration tool, not
from a guess here.

The result is a starting point you load, then calibrate on the wall. It is NOT a
baked mapping: every value is editable and git-versioned from the control node.

    python3 make_ring_model.py --nodes 12 --overlap 0.08 --out room-model.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def edge(width=0.0, gamma=2.2, black_lift=0.0):
    return {"width": round(width, 4), "gamma": gamma, "black_lift": black_lift}


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
    # Each node covers slice_w of the source; neighbours share `overlap` of it.
    # With N nodes wrapping a ring, the per-node source width is 1/N plus the
    # overlap shared on each side.
    slice_w = 1.0 / nodes
    src_w = slice_w + overlap  # node sees its slice plus the right overlap

    entries = []
    for i in range(nodes):
        name = f"pi-{i+1:02d}"
        x = i * slice_w
        # last node wraps to the seam at 1.0; for a non-ring strip it just stops
        has_left = ring or i > 0
        has_right = ring or i < nodes - 1
        entries.append({
            "node": name,
            "projector": f"P{i+1}",
            "source_region": {"x": round(x, 4), "y": 0.0,
                              "w": round(min(src_w, 1.0 - x) if not ring else src_w, 4),
                              "h": 1.0},
            "mesh": corner_pin_mesh(),
            "blend": {
                "left": edge(overlap if has_left else 0.0, 2.2, black_lift if has_left else 0.0),
                "right": edge(overlap if has_right else 0.0, 2.2, black_lift if has_right else 0.0),
                "top": edge(),
                "bottom": edge(),
            },
            "color": {"gain": [1.0, 1.0, 1.0], "gamma": 2.2, "lift": [0.0, 0.0, 0.0]},
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
                    help="overlap fraction of source width shared with neighbour")
    ap.add_argument("--black-lift", type=float, default=0.04)
    ap.add_argument("--strip", action="store_true",
                    help="open strip instead of a closed 360 ring")
    ap.add_argument("--out", type=Path, default=Path("room-model.ring.json"))
    args = ap.parse_args(argv)

    model = build(args.nodes, args.overlap, ring=not args.strip,
                  black_lift=args.black_lift)
    args.out.write_text(json.dumps(model, indent=2) + "\n")
    print(f"wrote {args.out} — {args.nodes} nodes, "
          f"{'ring' if not args.strip else 'strip'}, overlap {args.overlap}")
    print("load it as the control node's room-model, then calibrate on the wall.")


if __name__ == "__main__":
    main()
