"""Run an FS-Lite Feature Studio file locally and export the resulting geometry."""
from __future__ import annotations

import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import geom as G
from .geom import Context
from .interp import FeatureFn, Interpreter
from .lexer import FSSyntaxError
from .stdlib import StdLib
from .values import FSError, Id


@dataclass
class RunResult:
    ok: bool
    error: str | None = None
    error_line: int = 0
    context: Context | None = None
    output: list[str] = field(default_factory=list)
    feature: str | None = None
    params: list = field(default_factory=list)
    definition: dict = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def solids(self):
        return [b for b in self.context.bodies if b.kind == "solid"] if self.context else []

    def metrics(self) -> dict:
        """Volume (mm^3), area (mm^2), bbox (mm), body count: comparable with Onshape."""
        solids = self.solids
        if not solids:
            return {"bodies": 0, "volume_mm3": 0.0, "area_mm2": 0.0, "bbox_mm": None}
        vol = sum(G.props(b.shape, "volume").Mass() for b in solids) * 1e9
        area = sum(G.props(b.shape, "area").Mass() for b in solids) * 1e6
        lo, hi = G.bbox([b.shape for b in solids])
        return {"bodies": len(solids), "volume_mm3": round(vol, 4), "area_mm2": round(area, 4),
                "bbox_mm": [round(float(v) * 1000, 4) for v in list(lo) + list(hi)]}


def describe(res: RunResult) -> str:
    """Compact, LLM-readable summary of the built geometry."""
    from collections import Counter

    if not res.ok:
        return f"FAILED: {res.error}"
    m = res.metrics()
    lines = [f"solid bodies: {m['bodies']}"]
    if m["bodies"]:
        b = m["bbox_mm"]
        lines.append(f"bounding box (mm): x {b[0]:g}..{b[3]:g}, y {b[1]:g}..{b[4]:g}, z {b[2]:g}..{b[5]:g} "
                     f"(size {b[3]-b[0]:g} x {b[4]-b[1]:g} x {b[5]-b[2]:g})")
        lines.append(f"volume: {m['volume_mm3']:.1f} mm^3, surface area: {m['area_mm2']:.1f} mm^2")
    for i, body in enumerate(res.solids):
        faces = G.subshapes(body.shape, G.TopAbs_FACE)
        kinds = Counter(G.face_geom(f) for f in faces)
        cyl = Counter()
        for f in faces:
            if G.face_geom(f) == "CYLINDER":
                r = G.BRepAdaptor_Surface(G.TopoDS.Face(f)).Cylinder().Radius()
                cyl[round(2 * r * 1000, 3)] += 1
        lo, hi = G.bbox([body.shape])
        desc = f"body {i + 1}: {len(faces)} faces ({', '.join(f'{v} {k.lower()}' for k, v in kinds.most_common())})"
        desc += f", bbox {[round(float(v) * 1000, 2) for v in lo]}..{[round(float(v) * 1000, 2) for v in hi]}"
        if cyl:
            desc += "; cylindrical face diameters (mm): " + ", ".join(f"{d:g} x{n}" for d, n in sorted(cyl.items()))
        lines.append(desc)
    if res.params:
        lines.append("parameters: " + ", ".join(f"{p['name']}={res.definition.get(p['name'], p['default'])}"
                                               for p in res.params))
    if res.output:
        lines.append("log: " + " | ".join(res.output[:10]))
    return "\n".join(lines)


_INCH = re.compile(r'\d\s*(?:"|in\b|inch|inches\b|\'\')', re.I)
_MM = re.compile(r"\d\s*(?:mm|millimet|cm|centimet)\b|\b(?:mm|millimet\w*)\b", re.I)


def drawing_units(request: str) -> str:
    """'in' when the request gives sizes in inches (and no metric sizes come first), else 'mm'."""
    i, m = _INCH.search(request or ""), _MM.search(request or "")
    return "in" if i and (not m or i.start() < m.start()) else "mm"


def _drawing_title(text: str) -> str:
    """Request text wrapped to a few lines; blank lines dropped, long text cut with an ellipsis."""
    import textwrap
    lines = [w for ln in (text or "").splitlines() if ln.strip() for w in textwrap.wrap(ln.strip(), 90)]
    return "\n".join(lines[:3]) + ("\n…" if len(lines) > 3 else "")


_STEPS = {"mm": (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000),
          "in": (1 / 32, 1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 3, 6, 12, 24, 48)}


def tick_step(extent: float, units: str = "mm", target: int = 8) -> float:
    """Round tick spacing for an axis `extent` long: the smallest step giving at most `target` intervals."""
    steps = _STEPS.get(units, _STEPS["mm"])
    return next((s for s in steps if extent / s <= target), steps[-1])


def tick_label(v: float, step: float) -> str:
    """Tick text with only the decimals the step needs (0.25 -> '0.25', 1.0 -> '1'); no '-0'."""
    d = len(f"{step:.6f}".rstrip("0").split(".")[1]) if step < 1 else 0
    t = f"{v:.{d}f}"
    t = t.rstrip("0").rstrip(".") if "." in t else t
    return "0" if float(t) == 0 else t


_HOLE_MIN_SPAN = 5.0  # rad (~287 deg): a hole is (nearly) fully round; smaller concave spans are fillets/slots


def hole_features(solids) -> list[tuple[tuple, tuple, float]]:
    """Round holes as (center xyz m, unit axis xyz, diameter m).

    Rule: a CYLINDER face is concave (material outside) when its outward-from-material normal (surface normal,
    flipped for REVERSED faces) points toward the axis. Concave faces sharing axis line and radius are grouped
    (a through hole may be split in several faces, or at the seam) and kept only when their angular spans add
    up to >= ~287 deg, which drops concave fillets and partial slots. Bosses and pins are convex: never listed."""
    import numpy as np
    from OCP.BRepTools import BRepTools

    groups: dict = {}
    for b in solids:
        for f in G.subshapes(b.shape, G.TopAbs_FACE):
            if G.face_geom(f) != "CYLINDER":
                continue
            face = G.TopoDS.Face(f)
            surf = G.BRepAdaptor_Surface(face)
            cyl = surf.Cylinder()
            u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
            um, vm = (u0 + u1) / 2, (v0 + v1) / 2
            loc, d = cyl.Axis().Location(), cyl.Axis().Direction()
            o, ax_ = np.array([loc.X(), loc.Y(), loc.Z()]), np.array([d.X(), d.Y(), d.Z()])
            pt, du, dv = G.gp_Pnt(), G.gp_Vec(), G.gp_Vec()
            surf.D1(um, vm, pt, du, dv)
            nrm = np.cross([du.X(), du.Y(), du.Z()], [dv.X(), dv.Y(), dv.Z()])
            if face.Orientation() == 1:
                nrm = -nrm
            p = np.array([pt.X(), pt.Y(), pt.Z()])
            radial = p - (o + ax_ * np.dot(p - o, ax_))
            if np.dot(nrm, radial) >= 0:
                continue  # convex
            sgn = -1.0 if ax_[np.argmax(np.abs(ax_))] < 0 else 1.0  # canonical axis direction
            ax_ = ax_ * sgn
            perp = o - ax_ * np.dot(o, ax_)
            r = cyl.Radius()
            key = (tuple(np.round(ax_, 4)), tuple(np.round(perp, 6)), round(r, 6))
            g = groups.setdefault(key, {"span": 0.0, "lo": 1e9, "hi": -1e9, "ax": ax_, "perp": perp, "r": r})
            g["span"] += u1 - u0
            t0, t1 = np.dot(o, ax_) + sgn * v0, np.dot(o, ax_) + sgn * v1
            g["lo"], g["hi"] = min(g["lo"], t0, t1), max(g["hi"], t0, t1)
    out = []
    for g in groups.values():
        if g["span"] >= _HOLE_MIN_SPAN:
            c = g["perp"] + g["ax"] * (g["lo"] + g["hi"]) / 2
            out.append((tuple(float(x) for x in c), tuple(float(x) for x in g["ax"]), 2 * g["r"]))
    return out


def _fmt(v: float, dec: int) -> str:
    t = f"{v:.{dec}f}"
    return t.rstrip("0").rstrip(".") if "." in t else t


def hole_labels(holes, scale: float, units: str, axis: int, cap: int = 12) -> list[tuple[list, str]]:
    """(display-unit center, text) for holes whose axis is parallel to coordinate `axis` (0,1,2)."""
    dec = 2 if units == "in" else 1
    sel = [([c * scale for c in ctr], d * scale) for ctr, ax_, d in holes if abs(ax_[axis]) > 0.999]
    if len(sel) <= cap:
        return [(c, f"⌀{_fmt(d, dec)}") for c, d in sel]
    first: dict = {}
    for c, d in sel:
        k = round(d, 4)
        first.setdefault(k, [c, d, 0])[2] += 1
    return [(c, f"⌀{_fmt(d, dec)}" + (f" x{n}" if n > 1 else "")) for c, d, n in first.values()]


def _dim_line(ax, a, b, off, label: str, side: int, maxext: float) -> None:
    """Dimension line from a to b (3D, in the view plane) shifted by vector `off`, with extension lines,
    arrowheads and `label` at the middle (side=0, on the line) or beside it (side=1: along +off)."""
    import numpy as np
    a, b, off = np.array(a, float), np.array(b, float), np.array(off, float)
    u = (b - a) / np.linalg.norm(b - a)
    w = off / np.linalg.norm(off)
    pa, pb = a + off, b + off
    kw = dict(color="black", lw=0.8, zorder=20)
    gap, over = 0.008 * maxext, 0.012 * maxext
    for p, q in ((a, pa), (b, pb)):
        ax.plot(*zip(p + w * gap, q + w * over), **kw)
    ax.plot(*zip(pa, pb), **kw)
    ah = min(0.025 * maxext, np.linalg.norm(b - a) / 4)
    for p, s in ((pa, 1), (pb, -1)):
        for sg in (1, -1):
            ax.plot(*zip(p, p + s * u * ah + sg * w * ah / 3), **kw)
    mid = (pa + pb) / 2
    if side:
        mid = mid + w * 0.02 * maxext
    ax.text(*mid, label, fontsize=8, zorder=30, ha="center" if not side else "left",
            va="center", bbox=dict(boxstyle="square,pad=0.15", fc="white", ec="none", alpha=0.8))


def _dim_horizontal(ax, lo, hi, h: int, v: int, d: int, dec: int, dsign: int, maxext: float) -> None:
    """Overall dimensions in a view with horizontal world axis h and vertical axis v (depth axis d):
    width below the part, height right of it. Lines sit at the near side of the depth axis."""
    import numpy as np
    depth = lo[d] if dsign < 0 else hi[d]
    off = 0.06 * maxext

    def pt(hv, vv):
        p = np.zeros(3)
        p[h], p[v], p[d] = hv, vv, depth
        return p

    below = np.zeros(3); below[v] = -off
    right = np.zeros(3); right[h] = off
    _dim_line(ax, pt(lo[h], lo[v]), pt(hi[h], lo[v]), below, _fmt(hi[h] - lo[h], dec), 0, maxext)
    _dim_line(ax, pt(hi[h], lo[v]), pt(hi[h], hi[v]), right, _fmt(hi[v] - lo[v], dec), 1, maxext)


def _label_holes(ax, holes, scale, units, axis: int, depth: float) -> None:
    for c, txt in hole_labels(holes, scale, units, axis):
        c = list(c)
        c[axis] = depth
        ax.text(*c, txt, fontsize=7, zorder=30, ha="center", va="center",
                bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="0.5", lw=0.4, alpha=0.8))


def render_png(res: RunResult, path: Path, title: str = "", units: str = "mm") -> Path | None:
    """Four-view shaded preview (iso, front, top, right) using matplotlib; no GPU needed.

    units: 'mm' or 'in', for the axes and the overall size line."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    import numpy as np
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopLoc import TopLoc_Location

    if not res.solids:
        return None
    tris, base = [], []  # base: per-triangle RGB of the body's appearance color, or None
    for b in res.solids:
        n0 = len(tris)
        BRepMesh_IncrementalMesh(b.shape, 0.0002, False, 0.3, True)
        for f in G.subshapes(b.shape, G.TopAbs_FACE):
            loc = TopLoc_Location()
            tri = G.BRep_Tool.Triangulation_s(G.TopoDS.Face(f), loc)
            if tri is None:
                continue
            trsf = loc.Transformation()
            pts = [tri.Node(i).Transformed(trsf) for i in range(1, tri.NbNodes() + 1)]
            pts = np.array([[p.X(), p.Y(), p.Z()] for p in pts]) * 1000
            rev = G.TopoDS.Face(f).Orientation() == 1
            for i in range(1, tri.NbTriangles() + 1):
                a, b_, c = tri.Triangle(i).Get()
                tris.append(pts[[a - 1, c - 1, b_ - 1]] if rev else pts[[a - 1, b_ - 1, c - 1]])
        base += [b.color[:3] if b.color else (0.55, 0.68, 0.85)] * (len(tris) - n0)
    tris = np.array(tris) / (25.4 if units == "in" else 1.0)
    base = np.array(base, float)  # bodies without an appearance keep the default blue-grey
    lo, hi = tris.reshape(-1, 3).min(0), tris.reshape(-1, 3).max(0)
    ctr, span = (lo + hi) / 2, (hi - lo).max() / 2 * 1.3
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    from matplotlib.ticker import FuncFormatter, MultipleLocator
    step = tick_step(2 * span, units, 10 if units == "in" else 8)  # every axis spans 2*span (equal aspect), so one step fits all
    size = hi - lo
    dec = 0 if units == "mm" and size.min() >= 10 else (1 if units == "mm" else 2)
    maxext = size.max()
    scale = 1000 / (25.4 if units == "in" else 1.0)
    try:
        holes = hole_features(res.solids)
    except Exception:  # labels are a nicety; never lose the drawing over them
        holes = []
    fig = plt.figure(figsize=(10, 10))
    # (title, elev, azim, axis pointing at the viewer: hidden, its ticks would pile up into a scribble)
    views = [("iso", 25, -55, None), ("front (-Y)", 0, -90, "y"), ("top (+Z)", 90, -90, "z"), ("right (+X)", 0, 0, "x")]
    for k, (name, elev, azim, facing) in enumerate(views):
        ax = fig.add_subplot(2, 2, k + 1, projection="3d")
        if facing:
            ax.set_proj_type("ortho")
        e, a = np.radians(elev), np.radians(azim)
        view = np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])
        light = view * 0.8 + np.array([0.2, 0.1, 0.5])
        light /= np.linalg.norm(light)
        shade = 0.35 + 0.65 * np.clip(n @ light, 0, 1)
        colors = np.concatenate([shade[:, None] * base, np.ones((len(shade), 1))], 1)
        pc = Poly3DCollection(tris, facecolors=colors, edgecolor="none")
        ax.add_collection3d(pc)
        for i, axis in enumerate("xyz"):
            getattr(ax, f"set_{axis}lim")(ctr[i] - span, ctr[i] + span)
        ax.set_box_aspect((1, 1, 1))
        ax.view_init(elev=elev, azim=azim)
        ax.set_title(name)
        for axis in "xyz":
            a_ = getattr(ax, f"{axis}axis")
            if axis == facing:
                getattr(ax, f"set_{axis}ticks")([])
                continue
            a_.set_major_locator(MultipleLocator(step))
            a_.set_major_formatter(FuncFormatter(lambda v, _pos: tick_label(v, step)))
            getattr(ax, f"set_{axis}label")(f"{axis.upper()} ({units})")
        if facing == "y":
            _dim_horizontal(ax, lo, hi, 0, 2, 1, dec, -1, maxext)
        elif facing == "z":
            _dim_horizontal(ax, lo, hi, 0, 1, 2, dec, 1, maxext)
        if facing:
            _label_holes(ax, holes, scale, units, "xyz".index(facing), lo["xyz".index(facing)] if facing == "y" else hi["xyz".index(facing)])
    size = hi - lo
    size_line = "size: " + " × ".join(f"{v:.{dec}f}" for v in size) + f" {units}  (X × Y × Z)"
    fig.suptitle((_drawing_title(title) + "\n" if title else "") + size_line)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def run_source(src: str, feature: str | None = None, params: dict | None = None) -> RunResult:
    t0 = time.time()
    lib = StdLib()
    interp = Interpreter(lib.b)
    lib.interp = interp
    ctx = Context()
    res = RunResult(ok=False, context=ctx)
    try:
        interp.load(src)
        if not interp.features:
            raise FSError("no feature found: export a feature with `export const myPart = defineFeature(...)`")
        name = feature or next(iter(interp.features))
        if name not in interp.features:
            raise FSError(f"feature {name} not found; available: {', '.join(interp.features)}")
        res.feature = name
        ff, _ann = interp.features[name]
        definition, res.params = interp.collect_parameters(ff, params)
        res.definition = dict(definition)
        interp.call(ff, [ctx, Id(("F",)), definition], None)
        res.ok = True
    except (FSError, FSSyntaxError) as e:
        res.error = str(e)
        res.error_line = getattr(e, "line", 0)
    except RecursionError:
        res.error = "recursion too deep"
    except Exception as e:  # internal error: surface it but keep running
        res.error = f"internal error: {type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
    res.output = interp.output + ctx.log
    res.seconds = time.time() - t0
    return res


def feature_parameters(src: str, feature: str | None = None, overrides: dict | None = None):
    """(feature name, parameter list, values) without running any geometry."""
    lib = StdLib()
    interp = Interpreter(lib.b)
    lib.interp = interp
    interp.load(src)
    if not interp.features:
        raise FSError("no feature found: export a feature with `export const myPart = defineFeature(...)`")
    name = feature or next(iter(interp.features))
    definition, params = interp.collect_parameters(interp.features[name][0], overrides)
    return name, params, definition


def export(res: RunResult, out_dir: Path, stem: str = "part", formats=("step", "stl")) -> list[Path]:
    """Export solids in meters->millimeters (build123d works in mm by convention)."""
    import build123d as bd
    from OCP.gp import gp_Trsf
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform

    out_dir.mkdir(parents=True, exist_ok=True)
    solids = res.solids
    if not solids:
        return []
    t = gp_Trsf()
    t.SetScaleFactor(1000.0)
    shape = BRepBuilderAPI_Transform(G.compound([b.shape for b in solids]), t, True).Shape()
    comp = bd.Compound(shape)
    paths = []
    if "step" in formats:
        p = out_dir / f"{stem}.step"
        bd.export_step(comp, str(p))
        paths.append(p)
    if "stl" in formats:
        p = out_dir / f"{stem}.stl"
        bd.export_stl(comp, str(p), tolerance=0.01, angular_tolerance=0.1)
        paths.append(p)
    return paths
