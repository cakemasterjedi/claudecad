"""Pure 2D multicolor art helpers (shapely, mm, +y up): disjoint color layers, thin-band widening, flat-color SVG import.

Module level imports are shapely + stdlib only (svgelements is imported lazily in `from_svg`), so a generator script
can use this with a bare interpreter. See `fsgen.native.emboss` for the FeatureScript side.

A `Layer` is one color region of one piece of art: `color` (hex), `name` (color group / part name), `geometry`
(shapely, mm, in the art's own 2D frame) and `height` (mm above the base surface). Layers are listed from top
priority to bottom: in 3D, a lower layer must not overlap a higher one, `split` enforces that in 2D.
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field, replace

from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union

MITRE = dict(join_style="mitre", mitre_limit=4)
ROUND = dict(join_style="round", quad_segs=8)
SMOOTH = 0.04     # mm: round closing + opening radius that removes the stubs / notches a growth leaves
SLIVER = 0.03     # mm: gaps and spurs narrower than 2 x this left by a subtraction are closed / opened away
MIN_AREA = 0.01   # mm^2: smaller pieces left over by a subtraction are dropped (and recorded)
MIN_WIDTH = 0.45  # mm: default printable band width (about a 0.4 mm nozzle line)


@dataclass
class Layer:
    color: str          # "#rrggbb"
    name: str           # color group name (the part / 3MF object name, e.g. "Yellow")
    geometry: object    # shapely geometry, mm
    height: float       # mm above the base surface
    label: str = ""     # for reports (defaults to name)

    def __post_init__(self):
        if not self.label:
            self.label = self.name


@dataclass
class ThinBand:
    color: str
    name: str
    label: str
    area: float
    width: float        # approx. (2 * area / perimeter)
    bbox: tuple


@dataclass
class Report:
    """Collects the pieces `clean` dropped."""
    dropped: list = field(default_factory=list)   # (label, area)


def polys(g) -> list:
    """The Polygon parts of any shapely geometry."""
    if g is None or g.is_empty:
        return []
    if isinstance(g, Polygon):
        return [g]
    if isinstance(g, MultiPolygon):
        return list(g.geoms)
    return [p for x in getattr(g, "geoms", []) for p in polys(x)]


def clean(g, label: str = "", dropped: list | None = None):
    """Remove hairline slivers and notches a subtraction leaves along coincident edges (closing then opening by
    SLIVER, mitre joins), then drop pieces below MIN_AREA (recorded in `dropped`). Without this the 0.03 mm notches
    survive simplification as self-intersecting outlines (invalid solids)."""
    g = g.buffer(SLIVER, **MITRE).buffer(-2 * SLIVER, **MITRE).buffer(SLIVER, **MITRE)
    keep = []
    for p in polys(g):
        if p.area < MIN_AREA:
            if p.area > 0 and dropped is not None:
                dropped.append((label, p.area))
        else:
            keep.append(p)
    return unary_union(keep) if keep else Polygon()


def split(layers: list[Layer], dropped: list | None = None) -> list[Layer]:
    """layers from top priority to bottom: each loses the union of all higher ones (no overlap in 3D), cleaned."""
    out, above = [], Polygon()
    for ly in layers:
        g = ly.geometry
        out.append(replace(ly, geometry=clean(g.difference(above), ly.label, dropped) if not above.is_empty else g))
        above = unary_union([above, g])
    return out


def narrowest(g) -> float:
    """Width (mm) of the narrowest feature: per polygon, the smallest 2r for which a round opening of radius r
    removes more than 3 percent of that polygon's area (a strip or neck thinner than 2r shows up, rounded sharp
    corners do not). Capped at 9."""
    best = 9.0
    for p in polys(g):
        lo, hi = 0.0, 2.0
        for _ in range(14):
            r = (lo + hi) / 2
            if p.buffer(-r).buffer(r).area < p.area * 0.97:
                hi = r
            else:
                lo = r
        best = min(best, 2 * hi)
    return best


def _thin_pieces(p: Polygon, min_width: float):
    """(piece, approx width) for every thin band of polygon p: the part a round opening of radius min_width/2
    removes, minus tips and specks (area < 0.5 min_width^2, or shorter than 1.5 min_width)."""
    r = min_width / 2
    opened = p.buffer(-r).buffer(r)
    thin = p.difference(opened) if not opened.is_empty else p
    out = []
    for t in polys(thin):
        if t.area < 0.5 * min_width ** 2 or t.length <= 0:
            continue
        rect = t.minimum_rotated_rectangle
        if isinstance(rect, Polygon):
            c = list(rect.exterior.coords)
            if max(math.dist(c[0], c[1]), math.dist(c[1], c[2])) < 1.5 * min_width:
                continue    # shorter than 1.5 widths: a wedge / corner remnant, not a band
        w = 2 * t.area / t.length
        if w < min_width - 0.01:
            out.append((t, w))
    return out


def thin_report(layers: list[Layer], min_width: float = MIN_WIDTH) -> list[ThinBand]:
    """Bands narrower than `min_width` (tips and specks excluded by the area rule)."""
    out = []
    for ly in layers:
        for p in polys(ly.geometry):
            for t, w in _thin_pieces(p, min_width):
                out.append(ThinBand(ly.color, ly.name, ly.label, t.area, w, tuple(round(v, 2) for v in t.bounds)))
    return out


def _grown(t, parent, d: float):
    """Thin piece `t` of polygon `parent` grown by `d` mm. Round joins (a mitre join turns every sharp corner of a
    letter terminal into a spike) and never farther than `d` from the parent, so no spur can stick out."""
    return t.buffer(d, **ROUND).intersection(parent.buffer(d, **ROUND))


def widen(layers: list[Layer], min_width: float = MIN_WIDTH, dropped: list | None = None, passes: int = 8,
          carve: bool = False, absorb: bool = True, absorbed: list | None = None):
    """Thin-band widening on already split layers (top priority first). Each thin band is grown to `min_width`; a
    band may grow over lower layers or empty space, never over a higher layer; the grown area is removed from the
    lower layers (which are re-cleaned). A band is grown by half its width deficit per side and re-measured in the
    next pass, so a band whose one side is blocked by a higher layer still reaches `min_width` (the deficit halves
    per pass; `passes` bounds it). With `absorb` (default) a band that is still thin afterwards (enclosed by higher
    layers) is merged into the adjacent HIGHER layer sharing the longest boundary with it; each merge is appended to
    `absorbed` as (from name, to name, label, area). Returns (new layers, bands still thin). The result is disjoint
    again."""
    ls = list(layers)
    for _ in range(passes):
        changed = False
        for i in range(len(ls)):
            above = unary_union([x.geometry for x in ls[:i]]) if i else Polygon()
            grow = []
            for p in polys(ls[i].geometry):
                for t, w in _thin_pieces(p, min_width):
                    grow.append(_grown(t, p, (min_width - w) / 2 + 0.002))
            if not grow:
                continue
            changed = True
            g = unary_union([ls[i].geometry, *grow])
            g = g.buffer(SMOOTH, **ROUND).buffer(-2 * SMOOTH, **ROUND).buffer(SMOOTH, **ROUND)
            if not above.is_empty:
                g = g.difference(above)
            g = clean(g, ls[i].label, dropped)
            ls[i] = replace(ls[i], geometry=g)
            for j in range(i + 1, len(ls)):
                ls[j] = replace(ls[j], geometry=clean(ls[j].geometry.difference(g), ls[j].label, dropped))
        if not changed:
            break
    if carve:
        ls = _carve(ls, min_width, dropped)
    if absorb:
        ls = _absorb(ls, min_width, dropped, absorbed)
    return ls, thin_report(ls, min_width)


def _absorb(ls: list[Layer], min_width: float, dropped, absorbed: list | None) -> list[Layer]:
    """Merge each still-thin band into the higher layer it shares the longest border with (that layer gets slightly
    wider there, at its own height); then all lower layers lose the merged area, so the layers stay disjoint."""
    ls = list(ls)
    for _ in range(300):
        hit = None
        for i in range(1, len(ls)):
            for p in polys(ls[i].geometry):
                for t, w in _thin_pieces(p, min_width):
                    best, blen = None, 0.0
                    for j in range(i):
                        if ls[j].geometry.is_empty:
                            continue
                        n = t.boundary.intersection(ls[j].geometry.buffer(0.01)).length
                        if n > blen:
                            best, blen = j, n
                    if best is not None:
                        hit = (i, best, t)
                        break
                if hit:
                    break
            if hit:
                break
        if not hit:
            break
        i, j, t = hit
        above = unary_union([x.geometry for x in ls[:j]]) if j else Polygon()
        add = t.buffer(0.01, **MITRE)
        if not above.is_empty:
            add = add.difference(above)
        ls[j] = replace(ls[j], geometry=clean(unary_union([ls[j].geometry, add]), ls[j].label, dropped))
        for k in range(j + 1, len(ls)):
            ls[k] = replace(ls[k], geometry=clean(ls[k].geometry.difference(ls[j].geometry), ls[k].label, dropped))
        if absorbed is not None:
            absorbed.append((ls[i].name, ls[j].name, ls[i].label, round(t.area, 3)))
    return ls


def _score(ls, min_width):
    return sum(t.area * (min_width - t.width) / min_width for t in thin_report(ls, min_width))


def _carve(ls: list[Layer], min_width: float, dropped) -> list[Layer]:
    """Last resort for bands enclosed by higher layers (both sides blocked): the band takes area from ALL neighbours,
    higher ones included, but only when that lowers the total thin score (sum of area x deficit, so a neighbour that just
    becomes thin itself does not pay off). Not used unless `widen(carve=True)`."""
    for _ in range(40):
        count = _score(ls, min_width)
        accepted = False
        for i in range(len(ls)):
            for p in polys(ls[i].geometry):
                for t, w in _thin_pieces(p, min_width):
                    grow = _grown(t, p, (min_width - w) / 2 + 0.002)
                    trial = [replace(x, geometry=clean(x.geometry.difference(grow), x.label, dropped)) if j != i else x
                             for j, x in enumerate(ls)]
                    trial[i] = replace(ls[i], geometry=clean(unary_union([ls[i].geometry, grow]), ls[i].label, dropped))
                    if _score(trial, min_width) < count - 1e-4:
                        ls, accepted = trial, True
                        break
                if accepted:
                    break
            if accepted:
                break
        if not accepted:
            break
    return ls


# ---------- placement ----------
def union(layers: list[Layer]):
    return unary_union([ly.geometry for ly in layers])


def _map(layers, fn):
    return [replace(ly, geometry=fn(ly.geometry)) for ly in layers]


def fit(layers: list[Layer], width: float, center=(0.0, 0.0), mirror_u: bool = False) -> list[Layer]:
    """Scale all layers uniformly so their overall bounds are `width` mm wide, centred at `center` = (u, v);
    `mirror_u` flips u (left-right)."""
    b = union(layers).bounds
    k = width / (b[2] - b[0])
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2

    def f(g):
        g = affinity.translate(g, -cx, -cy)
        g = affinity.scale(g, -k if mirror_u else k, k, origin=(0, 0))
        return affinity.translate(g, center[0], center[1])
    return _map(layers, f)


def mirror(layers: list[Layer]) -> list[Layer]:
    """Mirror in u about u = 0."""
    return _map(layers, lambda g: affinity.scale(g, -1, 1, origin=(0, 0)))


def translate(layers: list[Layer], du: float, dv: float) -> list[Layer]:
    return _map(layers, lambda g: affinity.translate(g, du, dv))


# ---------- SVG import ----------
_NAMED = {  # name -> hex, nearest RGB wins
    "Black": "#111111", "White": "#f2f2f2", "Red": "#d62828", "Yellow": "#f5d90a", "Gold": "#d4a017",
    "Blue": "#1d4ed8", "Navy": "#0b1f4d", "Green": "#1f9d3a", "Purple": "#6b2fa0", "Silver": "#c9ccd1",
    "Gray": "#808080", "Orange": "#f08a00",
}


def _rgb(h: str):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def color_name(hex_color: str, names: dict | None = None) -> str:
    """Name for a hex color: `names` mapping first, else the nearest of a small table of common colors (if close
    enough, distance < 90 in RGB), else the hex itself."""
    h = hex_color.lower()
    if names:
        for k, v in names.items():
            if k.lower() == h:
                return v
    r = _rgb(h)
    best = min(_NAMED, key=lambda n: sum((a - b) ** 2 for a, b in zip(r, _rgb(_NAMED[n]))))
    d = math.dist(r, _rgb(_NAMED[best]))
    return best if d < 90 else h


def _flatten(subpath, px_per_mm: float):
    """Points (px) of one svgelements subpath."""
    from svgelements import Close, Line, Move
    pts = []
    for seg in subpath:
        if isinstance(seg, Move):
            if seg.end is not None:
                pts.append((seg.end.x, seg.end.y))
        elif isinstance(seg, (Line, Close)):
            if seg.end is not None:
                pts.append((seg.end.x, seg.end.y))
        else:
            n = min(400, max(8, int(seg.length(error=1e-3) / px_per_mm / 0.1)))
            for k in range(1, n + 1):
                q = seg.point(k / n)
                pts.append((q.x, q.y))
    return pts


def _fill_region(rings: list[list[tuple]], evenodd: bool):
    """Filled area of closed rings under the nonzero or even-odd rule (exact, via noding + winding per face)."""
    rings = [r for r in rings if len(r) >= 3 and abs(_signed(r)) > 1e-12]
    if not rings:
        return Polygon()
    if len(rings) == 1:
        return Polygon(rings[0]).buffer(0)
    lines = unary_union([Polygon(r).exterior for r in rings])
    sign = [1 if _signed(r) > 0 else -1 for r in rings]
    polys_in = [Polygon(r).buffer(0) for r in rings]
    faces = []
    for f in polygonize(lines):
        pt = f.representative_point()
        wnd = sum(s for s, q in zip(sign, polys_in) if q.contains(pt))
        cnt = sum(1 for q in polys_in if q.contains(pt))
        if (cnt % 2 == 1) if evenodd else (wnd != 0):
            faces.append(f)
    return unary_union(faces) if faces else Polygon()


def _signed(r):
    return 0.5 * sum(r[i][0] * r[(i + 1) % len(r)][1] - r[(i + 1) % len(r)][0] * r[i][1] for i in range(len(r)))


def from_svg(path, *, height: float = 0.6, names: dict | None = None, ppi: float = 96.0) -> list[Layer]:
    """Read a flat-color SVG into layers (mm, +y up, origin at the SVG's top-left corner, units from width/height
    and viewBox as svgelements resolves them; 1 px = 25.4/ppi mm). Every filled shape (path, polygon, rect, circle,
    ellipse) is a region; stroke-only shapes and text elements are ignored with a warning. Paint order is respected:
    a color's visible area is each of its shapes minus everything drawn after it, merged per color; layers are
    ordered by the topmost shape of each color (top priority first). All layers get `height`; `names` maps hex ->
    part name (see `color_name`)."""
    from svgelements import SVG, Path, Shape
    svg = SVG.parse(str(path), ppi=ppi)
    k = 25.4 / ppi
    shapes = []   # (hex, geometry in mm)
    skipped = set()
    for e in svg.elements():
        if not isinstance(e, Shape):
            if type(e).__name__ in ("Text", "Image"):
                skipped.add(type(e).__name__)
            continue
        fill = getattr(e, "fill", None)
        if fill is None or fill.value is None or (fill.alpha is not None and fill.alpha == 0):
            if getattr(e, "stroke", None) is not None and e.stroke.value is not None:
                skipped.add("stroke-only shape")
            continue
        p = Path(e)
        rings = []
        for sp in p.as_subpaths():
            pts = _flatten(sp, 1.0 / k)
            pts = [(x * k, -y * k) for x, y in pts]
            if len(pts) >= 3:
                rings.append(pts)
        g = _fill_region(rings, str(e.values.get("fill-rule", "nonzero")).strip() == "evenodd")
        if not g.is_empty:
            shapes.append(("#" + fill.hexrgb.lstrip("#").lower()[:6], g))
    for s in sorted(skipped):
        warnings.warn(f"{path}: {s} ignored (only filled shapes become geometry; convert text to paths first)")
    # painter's algorithm: visible = shape minus everything drawn after it
    cover = Polygon()
    vis = {}      # hex -> geometry
    top = {}      # hex -> index of the topmost shape
    for i in range(len(shapes) - 1, -1, -1):
        hx, g = shapes[i]
        v = g.difference(cover) if not cover.is_empty else g
        cover = unary_union([cover, g])
        top.setdefault(hx, i)
        if not v.is_empty:
            vis[hx] = unary_union([vis[hx], v]) if hx in vis else v
    order = sorted(vis, key=lambda h: -top[h])
    return [Layer(hx, color_name(hx, names), vis[hx], height) for hx in order]
