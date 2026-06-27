"""roommodel.py — the canonical, git-versioned room model.

The control node owns one room-model.json. It is the single source of truth for
every node's source_region, mesh, blend, and color. Each render node only ever
receives its own entry. Every save commits to git so a full hand calibration —
hours of work — is always recoverable, and a single bad edit can be reverted
without touching the other eleven nodes.
"""

from __future__ import annotations

import copy
import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

DEFAULT_PATH = Path(__file__).with_name("room-model.json")


def _default_edge() -> dict:
    return {"width": 0.0, "gamma": 2.2, "black_lift": 0.0}


def default_node_entry(node: str, projector: str = "") -> dict:
    """A flat 2x2 corner-pin identity entry — the general primitive at its
    lowest resolution. Subdividing the mesh is just more control points."""
    return {
        "node": node,
        "projector": projector or node.upper(),
        "source_region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
        "mesh": {
            "cols": 2,
            "rows": 2,
            "points": [
                {"u": 0.0, "v": 0.0, "x": -1.0, "y": 1.0},
                {"u": 1.0, "v": 0.0, "x": 1.0, "y": 1.0},
                {"u": 0.0, "v": 1.0, "x": -1.0, "y": -1.0},
                {"u": 1.0, "v": 1.0, "x": 1.0, "y": -1.0},
            ],
        },
        "blend": {
            "left": _default_edge(),
            "right": _default_edge(),
            "top": _default_edge(),
            "bottom": _default_edge(),
        },
        "color": {"gain": [1.0, 1.0, 1.0], "gamma": 2.2, "lift": [0.0, 0.0, 0.0]},
    }


class RoomModel:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.model: dict = self._load()

    # ---- load / save -----------------------------------------------------
    def _load(self) -> dict:
        if self.path.exists():
            with self.path.open() as f:
                return json.load(f)
        model = {"version": 0, "show": "", "media": "", "loop": True, "nodes": []}
        return model

    def reload(self) -> dict:
        with self._lock:
            self.model = self._load()
            return self.model

    def save(self, message: str, bump: bool = True, commit: bool = True) -> dict:
        """Write the model, optionally bump version, optionally git-commit."""
        with self._lock:
            if bump:
                self.model["version"] = int(self.model.get("version", 0)) + 1
            tmp = self.path.with_suffix(".json.tmp")
            with tmp.open("w") as f:
                json.dump(self.model, f, indent=2)
                f.write("\n")
            tmp.replace(self.path)
            if commit:
                self._git_commit(message)
            return self.model

    def _git_commit(self, message: str) -> None:
        repo = self.path.parent
        try:
            subprocess.run(
                ["git", "-C", str(repo), "add", self.path.name],
                check=True, capture_output=True,
            )
            r = subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", message],
                capture_output=True, text=True,
            )
            if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
                print(f"[roommodel] git commit warning: {r.stderr.strip()}")
        except FileNotFoundError:
            print("[roommodel] git not found; saved file but did not commit")
        except subprocess.CalledProcessError as e:
            print(f"[roommodel] git add failed: {e.stderr.decode().strip()}")

    # ---- queries ---------------------------------------------------------
    def nodes(self) -> list[str]:
        with self._lock:
            return [n["node"] for n in self.model.get("nodes", [])]

    def entry_for(self, node: str) -> Optional[dict]:
        with self._lock:
            for n in self.model.get("nodes", []):
                if n["node"] == node:
                    return copy.deepcopy(n)
        return None

    def ensure_node(self, node: str, projector: str = "") -> dict:
        with self._lock:
            for n in self.model.get("nodes", []):
                if n["node"] == node:
                    return n
            entry = default_node_entry(node, projector)
            self.model.setdefault("nodes", []).append(entry)
            return entry

    # ---- mutations (used by the calibration website) ---------------------
    def update_field(self, node: str, field: str, value: Any) -> Optional[dict]:
        """Replace one of mesh/blend/color/source_region on a node entry."""
        assert field in ("mesh", "blend", "color", "source_region")
        with self._lock:
            entry = self.ensure_node(node)
            entry[field] = copy.deepcopy(value)
            return copy.deepcopy(entry)

    def apply_mesh_delta(self, node: str, index: int, x: float, y: float) -> Optional[dict]:
        """Move a single mesh control point — the editor's drag primitive."""
        with self._lock:
            entry = self.ensure_node(node)
            pts = entry["mesh"]["points"]
            if 0 <= index < len(pts):
                pts[index]["x"] = x
                pts[index]["y"] = y
            return copy.deepcopy(entry)

    def set_mesh_resolution(self, node: str, cols: int, rows: int) -> dict:
        """Resample the node's mesh to cols x rows, preserving the current warp
        by bilinear interpolation of the existing control grid. Raising
        resolution adds steerable points only where a surface needs them."""
        with self._lock:
            entry = self.ensure_node(node)
            old = entry["mesh"]
            new_pts = []
            for r in range(rows):
                for c in range(cols):
                    u = c / (cols - 1)
                    v = r / (rows - 1)
                    x, y = _sample_mesh(old, u, v)
                    new_pts.append({"u": u, "v": v, "x": x, "y": y})
            entry["mesh"] = {"cols": cols, "rows": rows, "points": new_pts}
            return copy.deepcopy(entry)


def _sample_mesh(mesh: dict, u: float, v: float) -> tuple[float, float]:
    """Bilinear-sample an existing control grid at (u, v) -> (x, y)."""
    cols, rows = mesh["cols"], mesh["rows"]
    pts = mesh["points"]
    fc = u * (cols - 1)
    fr = v * (rows - 1)
    c0 = min(int(fc), cols - 2) if cols > 1 else 0
    r0 = min(int(fr), rows - 2) if rows > 1 else 0
    tc = fc - c0 if cols > 1 else 0.0
    tr = fr - r0 if rows > 1 else 0.0

    def P(c, r):
        p = pts[r * cols + c]
        return p["x"], p["y"]

    x00, y00 = P(c0, r0)
    x10, y10 = P(min(c0 + 1, cols - 1), r0)
    x01, y01 = P(c0, min(r0 + 1, rows - 1))
    x11, y11 = P(min(c0 + 1, cols - 1), min(r0 + 1, rows - 1))
    x = (x00 * (1 - tc) + x10 * tc) * (1 - tr) + (x01 * (1 - tc) + x11 * tc) * tr
    y = (y00 * (1 - tc) + y10 * tc) * (1 - tr) + (y01 * (1 - tc) + y11 * tc) * tr
    return x, y


if __name__ == "__main__":
    rm = RoomModel()
    print(f"loaded version {rm.model.get('version')} with nodes {rm.nodes()}")
