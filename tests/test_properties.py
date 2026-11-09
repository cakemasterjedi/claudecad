"""setProperty NAME / APPEARANCE: stored on bodies, kept through booleans, grouped on export (0 Onshape calls)."""
import re
import zipfile

import pytest

from fsgen.cli import main
from fsgen.native.local import build_local
from fsgen.native.script import trace

HEAD = 'FeatureScript 3083;\nimport(path : "onshape/std/common.fs", version : "3083.0");\n'


def box_script(extra: str, boxes=(("A", 0), ("B", 30), ("C", 60))) -> str:
    out = HEAD + "export function build(context is Context, id is Id)\n{\n"
    for nm, x in boxes:
        out += (f'    var s{nm} = newSketch(context, id + "{nm} sketch", {{ "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) }});\n'
                f'    skRectangle(s{nm}, "r", {{ "firstCorner" : vector({x}, 0) * millimeter, "secondCorner" : vector({x + 20}, 20) * millimeter }});\n'
                f'    skSolve(s{nm});\n'
                f'    extrude(context, id + "{nm}", {{ "entities" : qSketchRegion(id + "{nm} sketch"), "endBound" : BoundingType.BLIND,'
                f' "depth" : 5 * millimeter }});\n')
    return out + extra + "}\n"


def build(src):
    loc = build_local(trace(src))
    assert loc.ok, (loc.error, loc.warnings)
    return loc


def test_name_and_appearance_survive_a_boolean():
    extra = '''
    setProperty(context, { "entities" : qCreatedBy(id + "A", EntityType.BODY), "propertyType" : PropertyType.NAME, "value" : "Red" });
    setProperty(context, { "entities" : qCreatedBy(id + "A", EntityType.BODY), "propertyType" : PropertyType.APPEARANCE, "value" : color(1, 0, 0.5) });
    var h = newSketch(context, id + "hole sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skCircle(h, "c", { "center" : vector(10, 10) * millimeter, "radius" : 3 * millimeter });
    skSolve(h);
    extrude(context, id + "hole", { "entities" : qSketchRegion(id + "hole sketch"), "operationType" : NewBodyOperationType.REMOVE,
            "endBound" : BoundingType.BLIND, "depth" : 5 * millimeter, "defaultScope" : false,
            "booleanScope" : qCreatedBy(id + "A", EntityType.BODY) });
'''
    src = box_script(extra)
    loc = build(src)
    a = [b for b in loc.run.solids if b.name == "Red"]
    assert len(a) == 1 and a[0].color == pytest.approx((1, 0, 0.5, 1))
    from fsgen.fslite import geom as G
    assert G.props(a[0].shape, "volume").Mass() * 1e9 < 20 * 20 * 5 - 100   # the hole was cut from the named body
    assert sum(1 for b in loc.run.solids if b.name is None) == 2


def test_unknown_property_type_warns():
    extra = '''    setProperty(context, { "entities" : qCreatedBy(id + "A", EntityType.BODY), "propertyType" : PropertyType.MATERIAL, "value" : "x" });\n'''
    loc = build(box_script(extra))
    assert any("MATERIAL" in w for w in loc.warnings)


def test_color_range_is_checked():
    tr = trace(box_script('    setProperty(context, { "entities" : qCreatedBy(id + "A", EntityType.BODY), '
                          '"propertyType" : PropertyType.APPEARANCE, "value" : color(255, 0, 0) });\n'))
    assert not tr.ok and "0..1" in tr.error


def test_export_groups_same_name_solids(tmp_path, monkeypatch):
    names = ''.join(f'    setProperty(context, {{ "entities" : qCreatedBy(id + "{n}", EntityType.BODY), "propertyType" : PropertyType.NAME, "value" : "{v}" }});\n'
                    f'    setProperty(context, {{ "entities" : qCreatedBy(id + "{n}", EntityType.BODY), "propertyType" : PropertyType.APPEARANCE, "value" : color({c}) }});\n'
                    for n, v, c in (("A", "Gold", "1, 0.5, 0"), ("B", "Blue", "0, 0, 1"), ("C", "Gold", "1, 0.5, 0")))
    p = tmp_path / "grp.fs"
    p.write_text(box_script(names))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["studio", "build", str(p), "--formats", "step,stl,3mf"])
    assert e.value.code == 0
    d = tmp_path / "out" / "grp"
    assert sorted(f.name for f in d.glob("grp_*.stl")) == ["grp_Blue.stl", "grp_Gold.stl"]
    with zipfile.ZipFile(d / "grp.3mf") as z:
        xml = z.read("3D/3dmodel.model").decode()
    assert xml.count("<mesh>") == 2 and len(re.findall(r"<item ", xml)) == 2    # one object per name
    assert re.findall(r'<object [^>]*name="(\w+)"', xml) == ["Gold", "Blue"]
    assert "ff8000" in xml.lower() and "0000ff" in xml.lower()
    with zipfile.ZipFile(d / "grp_Gold.3mf") as z:
        assert z.read("3D/3dmodel.model").decode().count("<mesh>") == 1
    import build123d as bd
    gold = bd.Mesher().read(str(d / "grp_Gold.3mf"))
    assert abs(sum(s.volume for s in gold) - 2 * 20 * 20 * 5) < 1


def test_no_names_unchanged(tmp_path, monkeypatch):
    p = tmp_path / "plain.fs"
    p.write_text(box_script(""))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["studio", "build", str(p), "--formats", "stl,3mf"])
    assert e.value.code == 0
    d = tmp_path / "out" / "plain"
    assert sorted(f.name for f in d.glob("plain_*.stl")) == ["plain_part1.stl", "plain_part2.stl", "plain_part3.stl"]
    with zipfile.ZipFile(d / "plain.3mf") as z:
        xml = z.read("3D/3dmodel.model").decode()
    assert len(re.findall(r"<object [^>]*name=\"part\d\"", xml)) == 3
    assert "basematerials" not in xml
