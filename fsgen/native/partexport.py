"""Per-part and 3MF export of a multi-part build (print layout, mm). 0 Onshape calls."""
from __future__ import annotations

import locale
import re
from pathlib import Path

from ..fslite import geom as G
from .meshing import to_mm

FORMATS = ("step", "stl", "3mf")


def part_names(solids_m, spec: dict | None, base: Path) -> tuple[list[str], str]:
    """Name per built body: the fit file's part names (matched by their `at` point, as fit.place does), else part<N>.
    Returns (names, note); note is non-empty when the fit file could not be used."""
    names = [f"part{i}" for i in range(1, len(solids_m) + 1)]
    if not spec or not spec.get("parts"):
        return names, ""
    from .fit import FitError, place
    try:
        for p in place(solids_m, spec, base):
            if p.index is not None:
                names[p.index] = p.name
    except FitError as e:
        return [f"part{i}" for i in range(1, len(solids_m) + 1)], f"part names from the fit file not used: {e}"
    return names, ""


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name)


def _write_3mf(shapes_mm, names, path: Path) -> Path:
    import build123d as bd
    saved = locale.setlocale(locale.LC_ALL)     # lib3mf resets the process locale to "C" (breaks text encodings)
    try:
        mesher = bd.Mesher()
        for shape, name in zip(shapes_mm, names):
            solid = bd.Shape.cast(shape)
            solid.label = name
            mesher.add_shape(solid, linear_deflection=0.01, angular_deflection=0.1)
        mesher.write(str(path))
    finally:
        locale.setlocale(locale.LC_ALL, saved)
    return path


def export_parts(solids_m, names: list[str], out_dir: Path, stem: str, formats=("step", "stl", "3mf")) -> list[Path]:
    """One file per part and format (<stem>_<name>.<ext>), plus the combined <stem>.3mf with one object per part
    (STEP/STL combined files come from fslite.runner.export)."""
    import build123d as bd
    out_dir.mkdir(parents=True, exist_ok=True)
    shapes = [to_mm(s) for s in solids_m]
    paths = []
    if "3mf" in formats:
        paths.append(_write_3mf(shapes, names, out_dir / f"{stem}.3mf"))
    if len(shapes) > 1:
        for shape, name in zip(shapes, names):
            base = out_dir / f"{stem}_{_safe(name)}"
            comp = bd.Compound(G.compound([shape]))
            if "step" in formats:
                bd.export_step(comp, str(base.with_suffix(".step")))
                paths.append(base.with_suffix(".step"))
            if "stl" in formats:
                bd.export_stl(comp, str(base.with_suffix(".stl")), tolerance=0.01, angular_tolerance=0.1)
                paths.append(base.with_suffix(".stl"))
            if "3mf" in formats:
                paths.append(_write_3mf([shape], [name], base.with_suffix(".3mf")))
    return paths
