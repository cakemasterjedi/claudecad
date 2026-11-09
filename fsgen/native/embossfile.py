"""`fsgen emboss`: put flat-color SVG art onto an existing native Part Studio script as raised multicolor bodies.

Text level only (no Onshape): the generated block (see `emboss.Emboss`) is inserted at the end of the script's
`build` function body.

Base query: `qSubtraction(qAllModifiableSolidBodies(), qUnion([<every art body>]))`. The art bodies are known by feature
id (we generate them), so "the base" is everything else that exists. This is evaluated lazily, like any FS query, so it
stays correct however the file built its bodies (extrude NEW/ADD, booleans, patterns, an earlier `fsgen emboss` run).
fslite, the tracer, the paste writer and real Onshape all accept qSubtraction / qAllModifiableSolidBodies; a
`qUnion` of the file's own qCreatedBy ids would break for bodies that were later merged or split.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from . import colorlayers as cl
from .emboss import Emboss

PLANES = {"top": "Top", "front": "Front", "right": "Right"}
PLACEHOLDER = "@@EMBOSS_BASE@@"
_FUNC = re.compile(r"function\s*\w*\s*\(\s*context\s+is\s+Context\s*,\s*id\s+is\s+Id\b[^)]*\)\s*\{")


class EmbossError(Exception):
    pass


def find_body_end(src: str) -> int:
    """Index of the closing brace of the first `function (context is Context, id is Id ...) { ... }` (the `build`
    function, or the function passed to defineFeature)."""
    m = _FUNC.search(src)
    if not m:
        raise EmbossError("no build function found: expected `export function build(context is Context, id is Id) { ... }` "
                          "(or `defineFeature(function(context is Context, id is Id, definition is map) { ... })`)")
    depth, i, n = 1, m.end(), len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == '"':
            i += 1
            while i < n and src[i] != '"':
                i += 2 if src[i] == "\\" else 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise EmbossError("unbalanced braces in the build function")


def existing_ids(src: str) -> set[str]:
    return set(re.findall(r'\bid\s*\+\s*"([^"]*)"', src))


def unique_prefix(prefix: str, ids: set[str]) -> str:
    p, k = prefix, 1
    while any(i == p or i.startswith(p + " ") for i in ids):
        k += 1
        p = f"{prefix} {k}"
    return p


def parse_pairs(text: str | None, what: str, number: bool = False) -> dict:
    out = {}
    for item in [t for t in (text or "").split(",") if t.strip()]:
        if "=" not in item:
            raise EmbossError(f"{what}: expected KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        try:
            out[k.strip()] = float(v) if number else v.strip()
        except ValueError:
            raise EmbossError(f"{what}: {v!r} is not a number") from None
    return out


def norm_hex(h: str) -> str:
    h = h.strip().lower()
    if not h.startswith("#"):
        h = "#" + h
    if len(h) == 4:
        h = "#" + "".join(c * 2 for c in h[1:])
    if not re.fullmatch(r"#[0-9a-f]{6}", h):
        raise EmbossError(f"bad color {h!r} (use #rrggbb)")
    return h


@dataclass
class Options:
    plane: str = "top"
    offset: float = 0.0
    flip: bool = False
    width: float | None = None
    center: tuple = (0.0, 0.0)
    mirror: bool = False
    height: float = 0.6
    heights: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)
    min_width: float = cl.MIN_WIDTH
    skip: list = field(default_factory=list)
    cylinder: float | None = None
    axis_plane: str = "top"
    axis_center: tuple = (0.0, 0.0)
    standoff: float = 1.5
    depth_in: float = 3.0
    span: float = 200.0
    span_side: str = "both"        # both | normal | opposite
    base_name: str | None = None
    base_color: str | None = None
    prefix: str = "Emboss"


def prepare_layers(svg, o: Options) -> tuple[list, dict]:
    """SVG -> split, scaled/placed, widened layers with heights; returns (layers, report)."""
    layers = cl.from_svg(svg, height=o.height, names=o.names)
    if not layers:
        raise EmbossError(f"{svg}: no filled shapes found")
    rep = {"found": [(ly.name, ly.color, ly.geometry.area) for ly in layers]}
    skip = {norm_hex(h) for h in o.skip}
    unknown = skip - {ly.color.lower() for ly in layers}
    if unknown:
        raise EmbossError(f"--skip-color: no such color in the SVG: {', '.join(sorted(unknown))}; "
                          f"found {', '.join(ly.color for ly in layers)}")
    layers = [ly for ly in layers if ly.color.lower() not in skip]
    if not layers:
        raise EmbossError("every color was skipped")
    hs = {k.lower(): v for k, v in o.heights.items()}
    names = {ly.name.lower() for ly in layers}
    if bad := [k for k in hs if k not in names]:
        raise EmbossError(f"--heights: no color named {', '.join(bad)}; names: {', '.join(ly.name for ly in layers)}")
    layers = [replace(ly, height=hs.get(ly.name.lower(), ly.height)) for ly in layers]
    if len({(ly.name, ly.color.lower()) for ly in layers}) != len({ly.name for ly in layers}):
        raise EmbossError("two colors got the same name; give them distinct names with --names")
    b = cl.union(layers).bounds
    layers = cl.fit(cl.split(layers), o.width if o.width else b[2] - b[0], o.center, o.mirror)
    rep["bounds"] = cl.union(layers).bounds
    rep["before"] = cl.thin_report(layers, o.min_width) if o.min_width > 0 else []
    rep["absorbed"], rep["dropped"] = [], []
    if o.min_width > 0:
        layers, left = cl.widen(layers, o.min_width, rep["dropped"], absorbed=rep["absorbed"])
        rep["after"] = left
    else:
        rep["after"] = rep["before"]
    layers = [ly for ly in layers if not ly.geometry.is_empty]
    rep["groups"] = [(ly.name, ly.color, ly.geometry.area, ly.height) for ly in layers]
    return layers, rep


def emboss_block(layers, o: Options, ids: set[str], src: str = "") -> str:
    """The FS statements (indented 4 spaces) to append inside `build`."""
    prefix = unique_prefix(o.prefix, ids)
    em = Emboss(base=PLACEHOLDER)
    em.names |= ids
    stem = re.sub(r"\W", "", prefix) or "Emboss"
    while re.search(rf"\b{stem}Sk\d", src):
        stem += "x"
    em.var = f"{stem}Sk"        # sketch variables must not collide with the script's own (or an earlier run's)
    em.names |= {i[:-7] for i in ids if i.endswith(" sketch")}
    default = f'qCreatedBy(makeId("{PLANES[o.plane]}"), EntityType.FACE)'
    em.w(f"    // fsgen emboss: raised multicolor art ({', '.join(sorted({ly.name for ly in layers}))})")
    if o.cylinder is None:
        if o.offset:
            em.w(f'    cPlane(context, id + "{prefix} plane", {{ "entities" : {default}, "offset" : {abs(o.offset):.4f} * millimeter'
                 + (', "oppositeDirection" : true });' if o.offset < 0 else " });"))
            plane = f'qCreatedBy(id + "{prefix} plane", EntityType.FACE)'
        else:
            plane = default
        em.flat(prefix, plane, layers, opposite=o.flip)
    else:
        # --offset = signed distance of the cylinder's apex line from the plane (+ = on the plane's normal side);
        # the sketch plane stands `standoff` mm further out, prisms run back toward the surface.
        out = abs(o.offset) + o.standoff
        neg = o.offset < 0
        em.w(f'    cPlane(context, id + "{prefix} plane", {{ "entities" : {default}, "offset" : {out:.4f} * millimeter'
             + (', "oppositeDirection" : true });' if neg else " });"))
        plane = f'qCreatedBy(id + "{prefix} plane", EntityType.FACE)'
        em.cylinder(prefix, plane, layers, standoff=o.standoff, depth_in=o.depth_in, opposite=not neg,
                    axis_plane=f'qCreatedBy(makeId("{PLANES[o.axis_plane]}"), EntityType.FACE)',
                    axis_center=o.axis_center, radius=o.cylinder, span=o.span,
                    span_opposite=o.span_side == "opposite", span_both=o.span_side == "both")
    if o.base_name:
        em.properties(base=(o.base_name, norm_hex(o.base_color or "#808080")))
    else:
        em.properties()
    feats = [f for _, fs in em.groups.values() for f in fs]
    art = em.bodies_of(feats)
    base = f"qSubtraction(qAllModifiableSolidBodies(), {art})"
    return em.source().replace(PLACEHOLDER, base)


def apply(src: str, svg, o: Options) -> tuple[str, dict]:
    layers, rep = prepare_layers(svg, o)
    end = find_body_end(src)
    block = emboss_block(layers, o, existing_ids(src), src)
    head = src[:end].rstrip("\n")
    return head + "\n\n" + block + src[end:], rep
