"""`fsgen emboss`: SVG art onto an existing native script, built locally (0 Onshape calls)."""
import re
import zipfile

import pytest

from fsgen.cli import main
from fsgen.native import geomcheck
from fsgen.native.local import build_local
from fsgen.native.paste import paste_export
from fsgen.native.script import trace

HEAD = 'FeatureScript 3083;\nimport(path : "onshape/std/common.fs", version : "3083.0");\n'

BOX = (HEAD + "export function build(context is Context, id is Id)\n{\n"
       '    var s = newSketch(context, id + "Base sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });\n'
       '    skRectangle(s, "r", { "firstCorner" : vector(-15, -15) * millimeter, "secondCorner" : vector(15, 15) * millimeter });\n'
       '    skSolve(s);\n'
       '    extrude(context, id + "Base", { "entities" : qSketchRegion(id + "Base sketch"), "endBound" : BoundingType.BLIND, "depth" : 4 * millimeter });\n'
       "}\n")
R = 20.0
CYL = (HEAD + "export function build(context is Context, id is Id)\n{\n"
       '    var s = newSketch(context, id + "Barrel sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });\n'
       f'    skCircle(s, "c", {{ "center" : vector(0, 0) * millimeter, "radius" : {R} * millimeter }});\n'
       '    skSolve(s);\n'
       '    extrude(context, id + "Barrel", { "entities" : qSketchRegion(id + "Barrel sketch"), "endBound" : BoundingType.BLIND, "depth" : 30 * millimeter });\n'
       "}\n")
SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="20mm" viewBox="0 0 20 20">
<rect x="0" y="0" width="20" height="20" fill="#ffffff"/>
<circle cx="10" cy="10" r="9" fill="#000000"/>
<polygon points="10,3 12,8 17.5,8.2 13.2,11.6 14.8,17 10,13.8 5.2,17 6.8,11.6 2.5,8.2 8,8" fill="#f5d90a"/>
</svg>
"""


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "art.svg").write_text(SVG)
    (tmp_path / "box.fs").write_text(BOX)
    (tmp_path / "cyl.fs").write_text(CYL)
    return tmp_path


def run(*argv):
    try:
        main(["emboss", *argv])
    except SystemExit as e:   # main() exits with the command's code
        return e.code
    return 0


def build(path):
    loc = build_local(trace(path.read_text()))
    assert loc.ok, (loc.error, loc.warnings)
    return loc


def by_name(loc):
    out = {}
    for b in loc.run.solids:
        out.setdefault(b.name, []).append(b)
    return out


def test_flat_emboss_box(work, capsys):
    assert run("art.svg", "box.fs", "--plane", "top", "--offset", "4", "--width", "20", "--skip-color", "#ffffff") == 0
    text = capsys.readouterr().out
    assert "Yellow" in text and "Black" in text and "(skipped)" in text and "studio build" in text
    assert (work / "box.fs").read_text() == BOX                      # original untouched
    out = work / "box_emboss.fs"
    loc = build(out)
    groups = by_name(loc)
    assert {k for k in groups if k} == {"Yellow", "Black"}
    assert len(loc.run.solids) == 3                                 # base + one body per color (this art: 1 region each)
    assert geomcheck.overlaps(loc.run.solids) == []
    yellow = groups["Yellow"][0]
    assert yellow.color == pytest.approx((0xf5 / 255, 0xd9 / 255, 0x0a / 255, 1))


def test_3mf_objects_and_names(work):
    assert run("art.svg", "box.fs", "--offset", "4", "--base-name", "Plate", "--base-color", "#223344",
               "--skip-color", "#ffffff", "--names", "#f5d90a=Gold") == 0
    with pytest.raises(SystemExit) as e:
        main(["studio", "build", "box_emboss.fs", "--formats", "step,stl,3mf"])
    assert e.value.code == 0
    with zipfile.ZipFile(work / "out" / "box_emboss" / "box_emboss.3mf") as z:
        xml = z.read("3D/3dmodel.model").decode()
    assert sorted(re.findall(r'<object [^>]*name="(\w+)"', xml)) == ["Black", "Gold", "Plate"]


def test_default_names_without_base_name_still_group_colors(work):
    assert run("art.svg", "box.fs", "--offset", "4", "--skip-color", "#ffffff") == 0
    assert "Yellow" in by_name(build(work / "box_emboss.fs"))


def test_skip_color_and_heights(work):
    assert run("art.svg", "box.fs", "--offset", "4", "--heights", "Yellow=1.2") == 0
    loc = build(work / "box_emboss.fs")
    assert set(by_name(loc)) >= {"Yellow", "Black", "White"} and len(loc.run.solids) == 4
    import build123d as bd
    from fsgen.native.meshing import to_mm
    top = lambda n: bd.Compound(to_mm(by_name(loc)[n][0].shape)).bounding_box().max.Z
    assert top("Yellow") == pytest.approx(4 + 1.2, abs=0.01)
    assert top("Black") == pytest.approx(4 + 0.6, abs=0.01)
    assert top("White") == pytest.approx(4 + 0.6, abs=0.01)
    # skipping white (and the unknown-color error)
    assert run("art.svg", "box.fs", "-o", "b2.fs", "--offset", "4", "--skip-color", "ffffff") == 0
    assert "White" not in by_name(build(work / "b2.fs"))
    assert run("art.svg", "box.fs", "-o", "b3.fs", "--skip-color", "#123456") == 1


def test_flip_negative_offset(work):
    # art on the underside: plane z = 0 offset 0, extrude downward
    assert run("art.svg", "box.fs", "--flip", "--skip-color", "#ffffff", "--mirror") == 0
    loc = build(work / "box_emboss.fs")
    import build123d as bd
    from fsgen.native.meshing import to_mm
    assert bd.Compound(to_mm(by_name(loc)["Black"][0].shape)).bounding_box().min.Z == pytest.approx(-0.6, abs=0.01)


def test_running_twice_unique_ids(work):
    assert run("art.svg", "box.fs", "--offset", "4", "--width", "10", "--center", "-7,0", "--skip-color", "#ffffff") == 0
    assert run("art.svg", "box_emboss.fs", "-o", "box_emboss.fs", "--offset", "4", "--width", "10", "--center", "7,0",
               "--skip-color", "#ffffff") == 0
    src = (work / "box_emboss.fs").read_text()
    ids = re.findall(r'\bid\s*\+\s*"([^"]*)"', src)
    created = [i for i in ids if i not in ("Base sketch", "Base")]
    assert "Emboss 2 Yellow" in created and "Emboss Yellow" in created
    tr = trace(src)
    assert tr.ok, tr.error
    loc = build(work / "box_emboss.fs")
    assert len(by_name(loc)["Yellow"]) == 2 and len(loc.run.solids) == 5
    assert geomcheck.overlaps(loc.run.solids) == []


def test_paste_matches(work):
    assert run("art.svg", "box.fs", "--offset", "4", "--skip-color", "#ffffff", "--base-name", "Plate") == 0
    code, chk = paste_export(trace((work / "box_emboss.fs").read_text(), "studio"), "Emboss test")
    assert chk["ok"], chk
    assert chk["paste_parts"] == chk["native_parts"] == 3


def test_cylinder_follows_surface(work):
    import build123d as bd
    from fsgen.native.meshing import to_mm
    assert run("art.svg", "cyl.fs", "--cylinder", "20", "--plane", "right", "--offset", "20", "--center", "0,15",
               "--width", "16", "--skip-color", "#ffffff", "--heights", "Yellow=0.9") == 0
    loc = build(work / "cyl_emboss.fs")
    groups = by_name(loc)
    assert set(groups) >= {"Yellow", "Black"} and len(loc.run.solids) == 3
    assert geomcheck.overlaps(loc.run.solids) == []
    for name, h in (("Yellow", 0.9), ("Black", 0.6)):
        bb = bd.Compound(to_mm(groups[name][0].shape)).bounding_box()
        assert bb.max.X == pytest.approx(R + h, abs=0.01)           # apex at R + height
        # every vertex lies between the barrel and radius R + h
        for v in bd.Compound(to_mm(groups[name][0].shape)).vertices():
            assert (v.X ** 2 + v.Y ** 2) ** 0.5 <= R + h + 1e-3


def test_cylinder_negative_side(work):
    assert run("art.svg", "cyl.fs", "--cylinder", "20", "--plane", "right", "--offset", "-20", "--mirror",
               "--center", "0,15", "--width", "16", "--skip-color", "#ffffff") == 0
    import build123d as bd
    from fsgen.native.meshing import to_mm
    loc = build(work / "cyl_emboss.fs")
    assert bd.Compound(to_mm(by_name(loc)["Black"][0].shape)).bounding_box().min.X == pytest.approx(-(R + 0.6), abs=0.01)


def test_errors(work, capsys):
    (work / "bad.fs").write_text(HEAD + "export const x = 1;\n")
    assert run("art.svg", "bad.fs") == 1
    assert "no build function" in capsys.readouterr().out
    assert not (work / "bad_emboss.fs").exists()
    assert run("art.svg", "box.fs", "--heights", "Pink=1") == 1


def test_define_feature_style_and_comments(work):
    src = (HEAD + 'export const f = defineFeature(function(context is Context, id is Id, definition is map)\n{\n'
           '    // a } in a comment\n'
           '    var s = newSketch(context, id + "Base sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });\n'
           '    skRectangle(s, "r", { "firstCorner" : vector(-15, -15) * millimeter, "secondCorner" : vector(15, 15) * millimeter });\n'
           '    skSolve(s);\n'
           '    extrude(context, id + "Base", { "entities" : qSketchRegion(id + "Base sketch"), "endBound" : BoundingType.BLIND, "depth" : 4 * millimeter });\n'
           '});\n')
    from fsgen.native import embossfile as ef
    end = ef.find_body_end(src)
    assert src[end:].startswith("}\n);") or src[end:].startswith("}")
