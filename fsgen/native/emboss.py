"""FeatureScript emission for raised multicolor art (`colorlayers.Layer` lists) on a base part.

Pure text generation, importable with shapely only. The emitted calls are plain Onshape std (newSketch, skPolyline,
skCircle, extrude, booleanBodies, setProperty, color) that fslite builds locally and Onshape accepts.

    em = Emboss(base="ring")                      # base: FS query expression of the base bodies
    em.flat("Logo", 'qCreatedBy(id + "Floor", EntityType.FACE)', layers)       # plane sits on the surface
    em.cylinder("Side", plane, layers, standoff=1.5, depth_in=3.0, opposite=True,
                axis_plane=..., axis_center=(x, y), radius="#panelRadius", span=40)
    em.properties(base=("Ring", "#5b2a86"))       # one NAME + APPEARANCE per color group
    text = em.source()                            # statements, indented 4 spaces (inside `build`)

Art coordinates (u, v) are the sketch coordinates of the placement plane, in mm. Each layer is extruded NEW from
the plane (one body per connected region; polygons nested inside another's hole go into a second sketch because
qSketchRegion would drop them). Layers must be disjoint (`colorlayers.split`). Every placement also SUBTRACTS the
base (kept as a tool), so art never overlaps the base.
"""
from __future__ import annotations

import math

from shapely.geometry import Polygon
from shapely.ops import unary_union

from dataclasses import replace

from .colorlayers import Layer, clean, polys


def length(x) -> str:
    """FS length expression: numbers are millimeters, strings are used as they are."""
    return f"{x:.4f} * millimeter" if isinstance(x, (int, float)) else str(x)


def pt(x, y) -> str:
    return f"vector({x:.4f}, {y:.4f}) * millimeter"


def ring_points(coords):
    pts = []
    for x, y in coords:
        x, y = round(x, 4), round(y, 4)
        if not pts or math.hypot(x - pts[-1][0], y - pts[-1][1]) >= 0.005:
            pts.append((x, y))
    while len(pts) > 1 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 0.005:
        pts.pop()
    return pts


def rings_of(g, tol: float = 0.01) -> list[list[tuple]]:
    """Closed outlines (exterior and hole rings, simplified by `tol` mm) of a geometry, as point lists."""
    out = []
    for p in polys(g):
        p = p.simplify(tol) if tol else p
        for ring in [p.exterior, *p.interiors]:
            pts = ring_points(ring.coords)
            if len(pts) >= 3 and Polygon(pts).area > 0.002:
                out.append(pts)
    return out


def nesting_levels(g) -> list:
    """Split into groups so no polygon sits inside another polygon's hole in the same group
    (qSketchRegion(sk, true) drops regions inside holes, e.g. the island inside a zero)."""
    ps = polys(g)
    levels = []
    while ps:
        outer = [p for i, p in enumerate(ps)
                 if not any(i != j and any(Polygon(h).contains(p) for h in q.interiors) for j, q in enumerate(ps))]
        levels.append(unary_union(outer))
        ps = [p for p in ps if not any(p is o for o in outer)]
    return levels


def simplified_disjoint(layers: list[Layer], tol: float = 0.01) -> list[Layer]:
    """Simplify each layer by `tol` mm, lower layers losing what the simplified higher ones cover. Simplifying layers
    independently lets neighbors drift into each other by up to `tol` (sub-0.01 mm3 overlaps in the solids)."""
    out, above = [], None
    for ly in layers:
        g = ly.geometry.simplify(tol)
        if above is not None and not above.is_empty:
            g = clean(g.difference(above), ly.label).difference(above)   # clean() may close a notch back over `above`
        out.append(replace(ly, geometry=g))
        above = g if above is None else unary_union([above, g])
    return out


def hex_rgb(h: str):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


class Emboss:
    def __init__(self, base: str):
        self.base = base
        self.lines: list[str] = []
        self.names: set[str] = set()
        self.n = 0
        self.var = "s"      # prefix of the sketch variables (make it unique when appending to a script)
        self.groups: dict[str, list] = {}      # color name -> [hex, [extrude feature names]]
        self.regions: dict[str, list] = {}     # color name -> [(label, geometry)] for reports

    # ---- low level
    def w(self, s: str = ""):
        self.lines.append(s)

    def unique(self, name: str) -> str:
        base, k = name, 1
        while name in self.names:
            k += 1
            name = f"{base} {k}"
        self.names.add(name)
        return name

    def bodies_of(self, feats) -> str:
        return "qUnion([" + ", ".join(f'qCreatedBy(id + "{f}", EntityType.BODY)' for f in feats) + "])"

    def _sketch(self, name: str, plane: str, g):
        self.n += 1
        v = f"{self.var}{self.n}"
        self.w(f'    var {v} = newSketch(context, id + "{name}", {{ "sketchPlane" : {plane} }});')
        for i, ring in enumerate(rings_of(g, 0)):
            pts = [*ring, ring[0]]
            self.w(f'    skPolyline({v}, "r{i}", {{ "points" : [{", ".join(pt(x, y) for x, y in pts)}] }});')
        self.w(f"    skSolve({v});")

    def _prisms(self, nm: str, plane: str, ly: Layer, depth, opposite: bool) -> list[str]:
        """NEW extrudes of one layer (one per nesting level); returns the feature names."""
        feats = []
        for k, lv in enumerate(nesting_levels(ly.geometry)):
            fname = self.unique(f"{nm} {ly.label}" + ("" if k == 0 else f" inner {k}"))
            self._sketch(f"{fname} sketch", plane, lv)
            self.w(f'    extrude(context, id + "{fname}", {{ "entities" : qSketchRegion(id + "{fname} sketch", true),'
                   f' "operationType" : NewBodyOperationType.NEW, "endBound" : BoundingType.BLIND,'
                   f' "depth" : {depth}, "oppositeDirection" : {"true" if opposite else "false"} }});')
            feats.append(fname)
        if feats:
            grp = self.groups.setdefault(ly.name, [ly.color.lower(), []])
            if grp[0] != ly.color.lower():
                raise ValueError(f"color group {ly.name!r} has two colors: {grp[0]} and {ly.color}")
            grp[1].extend(feats)
            self.regions.setdefault(ly.name, []).append((f"{nm} {ly.label}", ly.geometry))
        return feats

    def _subtract_base(self, nm: str, feats: list[str]):
        if feats:
            self.w(f'    booleanBodies(context, id + "{self.unique(nm + " outside base")}", {{ "operationType" : '
                   f'BooleanOperationType.SUBTRACTION, "targets" : {self.bodies_of(feats)}, "tools" : {self.base}, '
                   f'"keepTools" : true }});')

    # ---- placements
    def flat(self, name: str, plane: str, layers: list[Layer], *, opposite: bool = False, subtract_base: bool = True):
        """Each layer extruded NEW from `plane` (an FS sketch-plane expression sitting on the target surface) by its
        height; `opposite` flips the extrude direction (use it when the plane normal points into the base)."""
        self.w(f"    // emboss (flat): {name}")
        feats = []
        for ly in simplified_disjoint(layers):
            feats += self._prisms(name, plane, ly, f"{ly.height} * millimeter", opposite)
        if subtract_base:
            self._subtract_base(name, feats)
        self.w()
        return feats

    def cylinder(self, name: str, plane: str, layers: list[Layer], *, standoff: float, depth_in: float,
                 opposite: bool, axis_plane: str, axis_center: tuple, radius, span: float,
                 span_opposite: bool = False, span_both: bool = False):
        """Art on a convex barrel (cylinder) surface so every layer's top follows the curve at a constant height.

        `plane` is a sketch plane `standoff` mm outside the surface apex; prisms of (standoff + depth_in) mm are
        extruded NEW toward the surface (`opposite` = the extrude direction opposes the plane normal). Per height, the
        prisms are trimmed with an INTERSECT extrude of a circle of radius `radius` + height around the cylinder axis
        (sketched on `axis_plane`, a plane perpendicular to the axis, at `axis_center` = (u, v) in its coordinates;
        extruded `span` mm, `span_opposite` flips the side, `span_both` extrudes `span` mm each way) scoped to those prisms only, then the base is subtracted.
        `radius`, and the center values, are mm numbers or FS length expressions."""
        self.w(f"    // emboss (cylinder): {name}: prisms from {standoff} mm outside the surface, trimmed to radius + height")
        depth = f"{standoff + depth_in} * millimeter"
        by_h: dict[float, list[str]] = {}
        allf = []
        for ly in simplified_disjoint(layers):
            f = self._prisms(name, plane, ly, depth, opposite)
            by_h.setdefault(ly.height, []).extend(f)
            allf += f
        for h, feats in sorted(by_h.items(), reverse=True):
            if not feats:
                continue
            tn = self.unique(f"{name} trim {h:g}")
            self.n += 1
            v = f"{self.var}{self.n}"
            self.w(f'    var {v} = newSketch(context, id + "{tn} sketch", {{ "sketchPlane" : {axis_plane} }});')
            self.w(f'    skCircle({v}, "trim", {{ "center" : vector({length(axis_center[0])}, {length(axis_center[1])}), '
                   f'"radius" : {length(radius)} + {h} * millimeter }});')
            self.w(f"    skSolve({v});")
            self.w(f'    extrude(context, id + "{tn}", {{ "entities" : qSketchRegion(id + "{tn} sketch"), '
                   f'"operationType" : NewBodyOperationType.INTERSECT, "endBound" : BoundingType.BLIND, '
                   f'"depth" : {span} * millimeter, "oppositeDirection" : {"true" if span_opposite else "false"}, '
                   + (f'"hasSecondDirection" : true, "secondDirectionBound" : BoundingType.BLIND, '
                      f'"secondDirectionDepth" : {span} * millimeter, ' if span_both else "") +
                   f'"defaultScope" : false, "booleanScope" : {self.bodies_of(feats)} }});')
        self._subtract_base(name, allf)
        self.w()
        return allf

    # ---- end
    def properties(self, base: tuple[str, str] | None = None):
        """setProperty NAME + APPEARANCE: the base bodies (name, hex) and one per color group."""
        self.w("    // part names + appearance, one per color (3MF object / AMS filament)")
        props = ([(base[0], base[1], self.base)] if base else [])
        props += [(nm, hx, self.bodies_of(f)) for nm, (hx, f) in self.groups.items()]
        for nm, hx, q in props:
            r, g, b = hex_rgb(hx)
            self.w(f'    setProperty(context, {{ "entities" : {q}, "propertyType" : PropertyType.NAME, "value" : "{nm}" }});')
            self.w(f'    setProperty(context, {{ "entities" : {q}, "propertyType" : PropertyType.APPEARANCE, '
                   f'"value" : color({r} / 255, {g} / 255, {b} / 255) }});')

    def source(self) -> str:
        return "\n".join(self.lines) + "\n"
