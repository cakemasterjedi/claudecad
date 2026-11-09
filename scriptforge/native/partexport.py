"""Per-part and 3MF export of a multi-part build (print layout, mm). 0 Onshape calls."""
from __future__ import annotations

import locale
import re
from pathlib import Path

from ..fslite import geom as G
from .meshing import to_mm

FORMATS = ("step", "stl", "3mf")


def part_names(solids_m, spec: dict | None, base: Path, set_names: list | None = None) -> tuple[list[str], str]:
    """Name per built body: the setProperty NAME (`set_names`, None where unset), overridden by the fit file's
    part names (matched by their `at` point, as fit.place does), else part<N>.
    Returns (names, note); note is non-empty when the fit file could not be used."""
    names = [(set_names[i] if set_names and set_names[i] else f"part{i + 1}") for i in range(len(solids_m))]
    if not spec or not spec.get("parts"):
        return names, ""
    from .fit import FitError, place
    try:
        for p in place(solids_m, spec, base):
            if p.index is not None:
                names[p.index] = p.name
    except FitError as e:
        return [(set_names[i] if set_names and set_names[i] else f"part{i + 1}") for i in range(len(solids_m))], \
            f"part names from the fit file not used: {e}"
    return names, ""


def group_by_name(shapes, names, colors=None) -> list[tuple[str, list, tuple | None]]:
    """Solids that share a name become one group (name, [shapes], color); order of first appearance.
    The group color is the first color set on one of its solids."""
    groups: dict = {}
    for i, (shape, name) in enumerate(zip(shapes, names)):
        g = groups.setdefault(name, [name, [], None])
        g[1].append(shape)
        if g[2] is None and colors and colors[i]:
            g[2] = tuple(colors[i])
    return [tuple(g) for g in groups.values()]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name)


def _object(shapes_mm, name: str, color=None):
    """One build123d shape for a group: the solid itself, or a compound of all its solids (one mesh object)."""
    import build123d as bd
    obj = bd.Shape.cast(shapes_mm[0]) if len(shapes_mm) == 1 else bd.Compound(G.compound(list(shapes_mm)))
    obj.label = name
    if color:
        obj.color = bd.Color(*color)
    return obj


def _write_3mf(groups, path: Path) -> Path:
    """groups: [(name, [shape_mm, ...], color or None)] -> one 3MF object per group."""
    import build123d as bd
    saved = locale.setlocale(locale.LC_ALL)     # lib3mf resets the process locale to "C" (breaks text encodings)
    try:
        mesher = bd.Mesher()
        for name, shapes, color in groups:
            mesher.add_shape(_object(shapes, name, color), linear_deflection=0.01, angular_deflection=0.1)
        mesher.write(str(path))
    finally:
        locale.setlocale(locale.LC_ALL, saved)
    return path


def export_parts(solids_m, names: list[str], out_dir: Path, stem: str, formats=("step", "stl", "3mf"),
                 colors: list | None = None) -> list[Path]:
    """One file per part group and format (<stem>_<name>.<ext>), plus the combined <stem>.3mf with one object per
    group (solids sharing a name are one object; `colors`: per-solid (r, g, b, a) or None). STEP/STL combined files
    come from fslite.runner.export."""
    import build123d as bd
    out_dir.mkdir(parents=True, exist_ok=True)
    shapes = [to_mm(s) for s in solids_m]
    groups = group_by_name(shapes, names, colors)
    paths = []
    if "3mf" in formats:
        paths.append(_write_3mf(groups, out_dir / f"{stem}.3mf"))
    if len(groups) > 1:
        for name, gshapes, color in groups:
            base = out_dir / f"{stem}_{_safe(name)}"
            comp = bd.Compound(G.compound(gshapes))
            if "step" in formats:
                bd.export_step(comp, str(base.with_suffix(".step")))
                paths.append(base.with_suffix(".step"))
            if "stl" in formats:
                bd.export_stl(comp, str(base.with_suffix(".stl")), tolerance=0.01, angular_tolerance=0.1)
                paths.append(base.with_suffix(".stl"))
            if "3mf" in formats:
                paths.append(_write_3mf([(name, gshapes, color)], base.with_suffix(".3mf")))
    return paths
