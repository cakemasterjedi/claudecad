"""Geometry checks before printing: is each part a valid closed solid, and are its walls thick enough? (0 Onshape calls)

validity: B-rep valid (BRepCheck), no free edges, positive volume, one solid, and the STL-level mesh is watertight
(every mesh edge used by exactly 2 triangles after merging vertices within 1e-6 mm).
thin_walls: wall thickness by casting a ray from random points on the surface along the inward normal against the
part's mesh (numpy Moller-Trumbore, 0.05 mm deflection, at most 4000 samples); the distance to the first exit
through the surface is the local thickness. Only head-on exits count (hit normal within 60 deg of the ray): near
fillets, chamfers and sharp edges a ray leaves at a grazing angle and would underestimate the wall, so those
samples are dropped instead of skipping a band along every edge. Entering hits are ignored, so a start point that
sits in a hole's facet gap (concave facets lie in the void) still measures the material behind it.
Thin samples are clustered (2 mm) into regions.
Inputs are solids in meters (the local builder's units), reports are in mm.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS
from OCP.OCP.collections import IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher as _EdgeFaceMap

from ..fslite import geom as G
from .meshing import face_triangles, to_mm

MERGE_MM = 1e-6


# ------------------------------------------------------------------ validity
@dataclass
class Validity:
    part: int                 # 1-based, in build order
    problems: list[str]
    solids: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def text(self) -> str:
        if self.ok:
            return (f"geometry check: part {self.part} ok (valid B-rep, closed, watertight mesh, "
                    f"{self.solids} solid{'' if self.solids == 1 else 's'})")
        return f"geometry check: part {self.part} PROBLEMS: " + "; ".join(self.problems)


def free_edges(shape) -> int:
    """Edges not shared by exactly 2 face sides (a seam is its face twice; degenerate edges are skipped)."""
    m = _EdgeFaceMap()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, m)
    bad = 0
    for i in range(1, m.Extent() + 1):
        edge = TopoDS.Edge(m.FindKey(i))
        if BRep_Tool.Degenerated_s(edge):
            continue
        bad += len(m.FindFromIndex(i)) != 2     # OCCT lists a seam's face twice, so it counts 2 like any edge
    return bad


def mesh_open_edges(shape_mm, deflection=0.05, angle=0.3) -> int:
    """Mesh edges not used by exactly 2 triangles, with vertices merged across faces."""
    tris = [t for _, t in face_triangles(shape_mm, deflection, angle) if len(t)]
    if not tris:
        return 0
    t = np.concatenate(tris)
    keys = np.round(t.reshape(-1, 3) / MERGE_MM).astype(np.int64)
    _, ids = np.unique(keys, axis=0, return_inverse=True)
    ids = ids.reshape(-1, 3)
    ids = ids[(ids[:, 0] != ids[:, 1]) & (ids[:, 1] != ids[:, 2]) & (ids[:, 0] != ids[:, 2])]   # collapsed at poles
    e = np.sort(np.concatenate([ids[:, [0, 1]], ids[:, [1, 2]], ids[:, [2, 0]]]), axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    return int((counts != 2).sum())


def validity(solids_m) -> list[Validity]:
    out = []
    for i, s in enumerate(solids_m, 1):
        shape = to_mm(s)
        problems = []
        n = len(G.subshapes(shape, TopAbs_SOLID))
        if n != 1:
            problems.append(f"{n} solids (expected 1)")
        if not BRepCheck_Analyzer(shape).IsValid():
            problems.append("invalid B-rep (BRepCheck)")
        fe = free_edges(shape)
        if fe:
            problems.append(f"{fe} open edge(s): shell not closed")
        if n and G.props(shape, "volume").Mass() <= 0:
            problems.append("volume not positive")
        try:
            me = mesh_open_edges(shape)
            if me:
                problems.append(f"mesh not watertight ({me} open or non-manifold edges)")
        except Exception as e:      # meshing a badly broken shape can fail outright
            problems.append(f"could not mesh ({e})")
        out.append(Validity(i, problems, n))
    return out


def validity_report(items: list[Validity]) -> str:
    return "\n".join(v.text() for v in items)


# ------------------------------------------------------------------ thin walls
@dataclass
class ThinWall:
    part: int
    min_mm: float
    area_mm2: float            # sampled surface area of the region (both faces of a wall count)
    center_mm: tuple
    samples: int = 0

    def text(self) -> str:
        x, y, z = self.center_mm
        return (f"{self.min_mm:5.2f} mm  ~{self.area_mm2:6.1f} mm2  at ({x:.1f}, {y:.1f}, {z:.1f})  part {self.part}")


def _sample_surface(tris, n, rng):
    """n random points on the mesh (area weighted) with the outward normal of the triangle each lies on."""
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    cr = np.cross(b - a, c - a)
    area = np.linalg.norm(cr, axis=1) / 2
    ok = area > 1e-9
    idx = np.flatnonzero(ok)
    p = area[idx] / area[idx].sum()
    pick = rng.choice(idx, size=n, p=p)
    r1, r2 = rng.random(n), rng.random(n)
    flip = r1 + r2 > 1
    r1, r2 = np.where(flip, 1 - r1, r1), np.where(flip, 1 - r2, r2)
    pts = a[pick] + (b[pick] - a[pick]) * r1[:, None] + (c[pick] - a[pick]) * r2[:, None]
    nrm = cr[pick] / (2 * area[pick])[:, None]
    return pts, nrm, float(area.sum())


def _first_exit(origins, dirs, tris, normals, chunk=64, eps=1e-3):
    """Distance along each ray to the first triangle it leaves the solid through (outward normal . dir > 0), and
    that triangle's |cos| with the ray. Moller-Trumbore against all triangles, chunked over rays."""
    a = tris[:, 0]
    e1, e2 = tris[:, 1] - a, tris[:, 2] - a
    out_t = np.full(len(origins), np.inf)
    out_c = np.zeros(len(origins))
    for s in range(0, len(origins), chunk):
        o, d = origins[s:s + chunk], dirs[s:s + chunk]
        h = np.cross(d[:, None, :], e2[None])                       # (r, m, 3)
        det = np.einsum("mk,rmk->rm", e1, h)
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / det
            sv = o[:, None, :] - a[None]
            u = np.einsum("rmk,rmk->rm", sv, h) * inv
            q = np.cross(sv, e1[None])
            v = np.einsum("rk,rmk->rm", d, q) * inv
            t = np.einsum("mk,rmk->rm", e2, q) * inv
            cosd = np.einsum("rk,mk->rm", d, normals)
            hit = (np.abs(det) > 1e-12) & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > eps) & (cosd > 0)
        t = np.where(hit, t, np.inf)
        j = t.argmin(1)
        r = np.arange(len(o))
        out_t[s:s + chunk] = t[r, j]
        out_c[s:s + chunk] = cosd[r, j]
    return out_t, out_c


def _cluster(points, radius):
    """Connected groups of points closer than radius (grid-hashed union-find)."""
    n = len(points)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cells: dict = {}
    for i, p in enumerate(points):
        cells.setdefault(tuple((p // radius).astype(int)), []).append(i)
    for (cx, cy, cz), members in cells.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in cells.get((cx + dx, cy + dy, cz + dz), ()):
                        for i in members:
                            if i < j and np.linalg.norm(points[i] - points[j]) <= radius:
                                parent[find(i)] = find(j)
    groups: dict = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def thin_walls(solids_m, min_mm=1.2, deflection=0.05, max_samples=4000, cluster_mm=2.0, seed=0) -> list[ThinWall]:
    """Regions where the wall is thinner than min_mm, thinnest first."""
    rng = np.random.default_rng(seed)
    found = []
    for part, s in enumerate(solids_m, 1):
        shape = to_mm(s)
        t = [x for _, x in face_triangles(shape, deflection, 0.3) if len(x)]
        if not t:
            continue
        tris = np.concatenate(t)
        cr = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        nrm = cr / np.maximum(np.linalg.norm(cr, axis=1, keepdims=True), 1e-12)
        total = float(np.linalg.norm(cr, axis=1).sum() / 2)
        n = int(min(max_samples, max(500, total / 0.5)))
        pts, pn, _ = _sample_surface(tris, n, rng)
        dist, cosd = _first_exit(pts - pn * 1e-3, -pn, tris, nrm)
        thick = dist + 1e-3
        thin = np.flatnonzero((thick < min_mm) & (cosd >= 0.5))
        if not len(thin):
            continue
        per_sample = total / n
        for g in _cluster(pts[thin], cluster_mm):
            idx = thin[g]
            found.append(ThinWall(part, round(float(thick[idx].min()), 2), round(len(idx) * per_sample, 1),
                                  tuple(round(float(v), 1) for v in pts[idx].mean(0)), len(idx)))
    found.sort(key=lambda w: (w.min_mm, -w.area_mm2))
    return found


def thin_wall_report(items: list[ThinWall], min_mm=1.2, top=8) -> str:
    if not items:
        return f"wall check: no walls thinner than {min_mm:g} mm"
    lines = [f"wall check: {len(items)} region(s) thinner than {min_mm:g} mm (thinnest first; "
             f"area counts both faces of a wall):"]
    lines += ["  " + w.text() for w in items[:top]]
    if len(items) > top:
        lines.append(f"  ... and {len(items) - top} more")
    return "\n".join(lines)
