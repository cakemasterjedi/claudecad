"""Cross-section of the assembled position: every part and component cut by a plane, drawn as filled 2D areas.

The plane comes from the fit file's [section] table (see fit.py). The cut is exact: each shape is intersected with the
half-space on the plane's far side, and the result's planar faces lying in the plane are triangulated (holes stay
holes). A thin slab instead of the half-space left revolved parts out: the boolean returned slivers, not a face.
2D coordinates: v = world Z projected into the plane (world Y when the plane is horizontal), u = n x v, so the
default normal (0, 1, 0) gives X to the right and Z up, seen from -Y. 0 Onshape calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.GeomAbs import GeomAbs_Plane
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from ..fslite import geom as G
from .meshing import face_triangles
from .viewer import PALETTE

PLANE_TOL_MM = 1e-6


@dataclass
class SectionPart:
    name: str
    color: str
    polygons: np.ndarray      # (n, 3, 2) triangles in plane coordinates (mm)

    @property
    def area_mm2(self) -> float:
        p = self.polygons
        a, b = p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]
        return float(abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]).sum() / 2)


def plane_axes(spec: dict | None):
    sec = (spec or {}).get("section") or {}
    origin = np.asarray(sec.get("origin", [0, 0, 0]), dtype=float)
    n = np.asarray(sec.get("normal", [0, 1, 0]), dtype=float)
    if origin.shape != (3,) or n.shape != (3,) or not np.linalg.norm(n):
        from .fit import FitError
        raise FitError("[section]: origin and normal must be 3 numbers (normal not zero)")
    n = n / np.linalg.norm(n)
    v = np.array([0.0, 0.0, 1.0]) if abs(n[2]) < 0.999 else np.array([0.0, 1.0, 0.0])
    v = v - n * (v @ n)
    v /= np.linalg.norm(v)
    return origin, n, np.cross(n, v), v


def _in_plane(face, origin, n) -> bool:
    s = BRepAdaptor_Surface(G.TopoDS.Face(face))
    if s.GetType() != GeomAbs_Plane:
        return False
    ax = s.Plane().Position()
    d, loc = ax.Direction(), ax.Location()
    return (abs(abs(d.X() * n[0] + d.Y() * n[1] + d.Z() * n[2]) - 1) < 1e-9
            and abs((np.array([loc.X(), loc.Y(), loc.Z()]) - origin) @ n) < PLANE_TOL_MM)


def section(placed, spec: dict | None) -> list[SectionPart]:
    """Cut every placed shape (mm, assembled position); shapes the plane misses are left out."""
    origin, n, u, v = plane_axes(spec)
    lo, hi = G.bbox([p.shape for p in placed])
    size = float(np.linalg.norm(hi - lo)) * 2 + 10
    corner = origin + (v - u) * size / 2        # the box grows along u and (Ax2 is right-handed) along n x u = -v
    half = BRepPrimAPI_MakeBox(gp_Ax2(gp_Pnt(*corner), gp_Dir(*n), gp_Dir(*u)), size, size, size).Shape()
    out = []
    for i, p in enumerate(placed):
        cut = BRepAlgoAPI_Common(p.shape, half)
        if not cut.IsDone() or not G.subshapes(cut.Shape(), G.TopAbs_FACE):
            continue
        tris = []
        for f, t in face_triangles(cut.Shape(), 0.02, 0.15):
            if len(t) and _in_plane(f, origin, n):
                tris.append(np.stack([(t - origin) @ u, (t - origin) @ v], axis=-1))
        if tris:
            out.append(SectionPart(p.name, p.color or PALETTE[i % len(PALETTE)], np.concatenate(tris)))
    return out


def _axis_label(a: np.ndarray) -> str:
    for k, name in enumerate("XYZ"):
        if abs(abs(a[k]) - 1) < 1e-6:
            return f"{name} (mm)" if a[k] > 0 else f"-{name} (mm)"
    return "in-plane " + np.array2string(np.round(a, 2)) + " (mm)"


def render_section(parts: list[SectionPart], spec: dict | None, path: Path, title: str = "") -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from matplotlib.patches import Patch

    origin, n, u, v = plane_axes(spec)
    fig, ax = plt.subplots(figsize=(9, 7), dpi=110)
    for p in parts:
        ax.add_collection(PolyCollection(p.polygons, facecolors=p.color, edgecolors=p.color, linewidths=0.4))
    if parts:
        pts = np.concatenate([p.polygons.reshape(-1, 2) for p in parts])
        lo, hi = pts.min(0), pts.max(0)
        pad = max(float((hi - lo).max()) * 0.05, 1.0)
        ax.set_xlim(lo[0] - pad, hi[0] + pad)
        ax.set_ylim(lo[1] - pad, hi[1] + pad)
        ax.legend(handles=[Patch(facecolor=p.color, label=f"{p.name} ({p.area_mm2:.0f} mm2)") for p in parts],
                  loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8)
    ax.set_aspect("equal")
    ax.grid(True, lw=0.3, alpha=0.5)
    ax.set_xlabel(_axis_label(u))
    ax.set_ylabel(_axis_label(v))
    ax.set_title(f"{title + ': ' if title else ''}section through ({', '.join(f'{x:g}' for x in origin)}), "
                 f"normal ({', '.join(f'{x:g}' for x in n)}), seen from the -normal side", fontsize=9)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path
