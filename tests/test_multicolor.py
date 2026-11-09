"""Multicolor parts: paste export of setProperty, overlap check of named bodies (0 Onshape calls)."""
import re
from pathlib import Path

from scriptforge.native import geomcheck
from scriptforge.native.local import build_local
from scriptforge.native.paste import paste_export
from scriptforge.native.script import trace

ROOT = Path(__file__).resolve().parent.parent
HEAD = 'FeatureScript 3083;\nimport(path : "onshape/std/common.fs", version : "3083.0");\n'


def test_paste_contains_setproperty_in_feature_order():
    code, chk = paste_export(trace((ROOT / "examples/native/multicolor_tag.fs").read_text(), "studio"), "Tag")
    assert chk["ok"], chk
    lines = code.splitlines()
    names = [l for l in lines if "PropertyType.NAME" in l]
    colors = [l for l in lines if "PropertyType.APPEARANCE" in l]
    assert len(names) == 3 and len(colors) == 3
    assert '"value" : "Yellow"' in names[1] and 'qCreatedBy(id + "disc", EntityType.BODY)' in names[1]
    assert "color(1, 0.8, 0)" in colors[1] and "color(0.1, 0.1, 0.1)" in colors[0]
    assert all(l.lstrip().startswith("setProperty(context,") for l in names + colors)
    last_extrude = max(i for i, l in enumerate(lines) if "opExtrude" in l)
    assert min(i for i, l in enumerate(lines) if "setProperty(" in l) > last_extrude   # script sets them at the end


def test_paste_property_between_features():
    src = HEAD + '''export function build(context is Context, id is Id)
{
    var a = newSketch(context, id + "A sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skRectangle(a, "r", { "firstCorner" : vector(0, 0) * millimeter, "secondCorner" : vector(10, 10) * millimeter });
    skSolve(a);
    extrude(context, id + "A", { "entities" : qSketchRegion(id + "A sketch"), "endBound" : BoundingType.BLIND, "depth" : 5 * millimeter });
    setProperty(context, { "entities" : qCreatedBy(id + "A", EntityType.BODY), "propertyType" : PropertyType.NAME, "value" : "Red" });
    var b = newSketch(context, id + "B sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skRectangle(b, "r", { "firstCorner" : vector(20, 0) * millimeter, "secondCorner" : vector(30, 10) * millimeter });
    skSolve(b);
    extrude(context, id + "B", { "entities" : qSketchRegion(id + "B sketch"), "endBound" : BoundingType.BLIND, "depth" : 5 * millimeter });
}
'''
    code, chk = paste_export(trace(src, "studio"), "T")
    assert chk["ok"], chk
    lines = code.splitlines()
    p = next(i for i, l in enumerate(lines) if "setProperty(" in l)
    assert any('opExtrude(context, id + "a" + "tool"' in l for l in lines[:p])
    assert not any('id + "b" + "tool"' in l for l in lines[:p])
    assert any('id + "b" + "tool"' in l for l in lines[p:])


def test_paste_without_properties_has_no_setproperty():
    code, _ = paste_export(trace((ROOT / "examples/native/plate.fs").read_text(), "studio"), "Plate")
    assert "setProperty" not in code


def two_boxes(x2, names=True):
    out = HEAD + "export function build(context is Context, id is Id)\n{\n"
    for nm, x in (("A", 0), ("B", x2)):
        out += (f'    var s{nm} = newSketch(context, id + "{nm} sketch", {{ "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) }});\n'
                f'    skRectangle(s{nm}, "r", {{ "firstCorner" : vector({x}, 0) * millimeter, "secondCorner" : vector({x + 10}, 10) * millimeter }});\n'
                f'    skSolve(s{nm});\n'
                f'    extrude(context, id + "{nm}", {{ "entities" : qSketchRegion(id + "{nm} sketch"), "operationType" : NewBodyOperationType.NEW,'
                f' "endBound" : BoundingType.BLIND, "depth" : 5 * millimeter }});\n')
        if names:
            out += (f'    setProperty(context, {{ "entities" : qCreatedBy(id + "{nm}", EntityType.BODY), '
                    f'"propertyType" : PropertyType.NAME, "value" : "Color{nm}" }});\n')
    return out + "}\n"


def check(src):
    loc = build_local(trace(src))
    assert loc.ok, loc.error
    return geomcheck.overlaps(loc.run.solids)


def test_overlapping_named_boxes_are_reported():
    items = check(two_boxes(5))
    assert len(items) == 1 and abs(items[0].volume_mm3 - 5 * 10 * 5) < 0.01
    text = geomcheck.overlap_report(items)
    assert "part 1 (ColorA)" in text and "part 2 (ColorB)" in text


def test_touching_and_separate_boxes_are_not_reported():
    assert check(two_boxes(10)) == []
    assert check(two_boxes(30)) == []


def test_unnamed_bodies_are_not_checked():
    assert check(two_boxes(5, names=False)) == []


def test_paste_intersect_over_several_bodies_keeps_each():
    """extrude INTERSECT scoped to several disjoint bodies trims each one (std: SUBTRACT_COMPLEMENT); a plain
    INTERSECTION of all scope bodies would be empty."""
    src = HEAD + '''export function build(context is Context, id is Id)
{
    var a = newSketch(context, id + "A sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skRectangle(a, "r1", { "firstCorner" : vector(0, 0) * millimeter, "secondCorner" : vector(10, 10) * millimeter });
    skRectangle(a, "r2", { "firstCorner" : vector(20, 0) * millimeter, "secondCorner" : vector(30, 10) * millimeter });
    skSolve(a);
    extrude(context, id + "A", { "entities" : qSketchRegion(id + "A sketch"), "endBound" : BoundingType.BLIND, "depth" : 5 * millimeter });
    var t = newSketch(context, id + "T sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skRectangle(t, "r", { "firstCorner" : vector(-5, -5) * millimeter, "secondCorner" : vector(35, 5) * millimeter });
    skSolve(t);
    extrude(context, id + "T", { "entities" : qSketchRegion(id + "T sketch"), "operationType" : NewBodyOperationType.INTERSECT,
            "endBound" : BoundingType.BLIND, "depth" : 5 * millimeter, "defaultScope" : false,
            "booleanScope" : qCreatedBy(id + "A", EntityType.BODY) });
}
'''
    tr = trace(src, "studio")
    res = build_local(tr)
    assert res.ok and len(res.run.solids) == 2
    code, chk = paste_export(tr, "T")
    assert chk["ok"], chk
    assert "SUBTRACT_COMPLEMENT" in code
