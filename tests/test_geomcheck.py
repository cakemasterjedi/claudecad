"""Geometry checks (validity, thin walls), per-part/3MF export, section PNG and exploded scene. 0 Onshape calls."""
import base64
import json
import locale
import re
import tomllib
import zipfile
from pathlib import Path

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing, BRepBuilderAPI_Transform
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt, gp_Trsf

from fsgen.fslite import geom as G
from fsgen.native.fit import explode, place
from fsgen.native.geomcheck import thin_wall_report, thin_walls, validity, validity_report
from fsgen.native.local import build_local
from fsgen.native.script import trace
from fsgen.native.section import section

ROOT = Path(__file__).resolve().parent.parent
BOX = ROOT / "examples/native/fit/lidded_box.fs"
SPEC = tomllib.loads((ROOT / "examples/native/fit/lidded_box.fit.toml").read_text())


def meters(shape_mm):
    t = gp_Trsf()
    t.SetScaleFactor(0.001)
    return BRepBuilderAPI_Transform(shape_mm, t, True).Shape()


def block(x, y, z, dx, dy, dz):
    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()


def local_solids(name):
    loc = build_local(trace((ROOT / "examples/native" / name).read_text()))
    assert loc.ok and not loc.partial, (loc.error, loc.warnings)
    return [b.shape for b in loc.run.solids]


# ---------------------------------------------------------------- validity
def test_validity_ok_on_good_solids():
    items = validity([meters(block(0, 0, 0, 10, 20, 30))] + local_solids("plate.fs"))
    assert all(v.ok for v in items), validity_report(items)
    assert validity_report(items).splitlines()[0] == \
        "geometry check: part 1 ok (valid B-rep, closed, watertight mesh, 1 solid)"


def test_validity_flags_a_shell_with_a_face_missing():
    faces = G.subshapes(block(0, 0, 0, 10, 10, 10), G.TopAbs_FACE)[:5]
    sewing = BRepBuilderAPI_Sewing(1e-6)
    for f in faces:
        sewing.Add(f)
    sewing.Perform()
    (v,) = validity([meters(sewing.SewedShape())])
    assert not v.ok
    text = v.text()
    assert "PROBLEMS" in text and "open edge" in text and "0 solids" in text


# ---------------------------------------------------------------- thin walls
def test_thin_web_is_found_and_a_3mm_plate_is_not():
    web = BRepAlgoAPI_Fuse(block(0, 0, 0, 30, 30, 3), block(14, 0, 3, 0.8, 30, 10)).Shape()
    found = thin_walls([meters(web)])
    assert found and found[0].min_mm == pytest.approx(0.8, abs=0.1)
    assert 8 < found[0].center_mm[2] < 10 or 3 <= found[0].center_mm[2] <= 13
    assert "wall check: " in thin_wall_report(found) and "thinner than 1.2 mm" in thin_wall_report(found)
    assert thin_walls([meters(block(0, 0, 0, 30, 30, 3))]) == []
    assert thin_wall_report([]) == "wall check: no walls thinner than 1.2 mm"


@pytest.mark.parametrize("name", ["plate.fs", "flange.fs", "l_bracket.fs", "enclosure.fs"])
def test_no_false_thin_regions_near_holes_and_fillets(name):
    assert thin_walls(local_solids(name)) == []


def test_min_wall_threshold_is_respected():
    plate = [meters(block(0, 0, 0, 30, 30, 3))]
    assert thin_walls(plate, min_mm=3.5)          # 3 mm is thinner than 3.5 mm
    assert thin_walls(plate, min_mm=2.5) == []


# ---------------------------------------------------------------- exports
def test_cli_build_writes_per_part_files_and_3mf(tmp_path, monkeypatch, capsys):
    from fsgen.cli import main
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["studio", "build", str(BOX), "--formats", "step,stl,3mf"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    d = tmp_path / "out/lidded_box"
    for stem in ("lidded_box", "lidded_box_box", "lidded_box_lid"):
        for ext in ("step", "stl", "3mf"):
            assert (d / f"{stem}.{ext}").stat().st_size > 0, f"{stem}.{ext}"
    assert "geometry check: part 2 ok" in out and "wall check: no walls thinner than 1.2 mm" in out
    for f, n in (("lidded_box.3mf", 2), ("lidded_box_lid.3mf", 1)):
        with zipfile.ZipFile(d / f) as z:
            model = z.read("3D/3dmodel.model").decode()
        assert model.count("<mesh>") == n and len(re.findall(r"<item ", model)) == n   # one object per part
    assert locale.getpreferredencoding(False).lower().startswith("utf")   # lib3mf must not leave the C locale
    with zipfile.ZipFile(d / "lidded_box.3mf") as z:
        model = z.read("3D/3dmodel.model").decode()
    assert 'name="box"' in model and 'name="lid"' in model and 'unit="millimeter"' in model


def test_default_formats_have_no_3mf_and_unnamed_parts(tmp_path, monkeypatch):
    from fsgen.cli import main
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "two.fs"
    src.write_text(BOX.read_text())            # no two.fit.toml next to it: parts are named part<N>
    with pytest.raises(SystemExit):
        main(["studio", "build", str(src)])
    names = sorted(p.name for p in (tmp_path / "out/two").iterdir())
    assert "two_part1.step" in names and "two_part2.stl" in names and "two.step" in names
    assert not any(n.endswith(".3mf") for n in names)


def test_cli_rejects_unknown_format(tmp_path, monkeypatch, capsys):
    from fsgen.cli import main
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["studio", "build", str(BOX), "--formats", "obj"])
    assert e.value.code == 1 and "unknown obj" in capsys.readouterr().out


# ---------------------------------------------------------------- section and explode
@pytest.fixture(scope="module")
def placed():
    loc = build_local(trace(BOX.read_text()))
    return place([b.shape for b in loc.run.solids], SPEC, BOX.parent)


def test_section_has_polygons_for_both_parts_and_misses_when_off_the_parts(placed):
    cut = {c.name: c for c in section(placed, SPEC)}
    assert {"box", "lid", "card"} <= set(cut) and all(len(c.polygons) for c in cut.values())
    assert cut["box"].area_mm2 == pytest.approx(30 * 15 - 26 * 13, rel=1e-3)     # U-shaped wall at y = 0
    assert cut["lid"].polygons[..., 0].min() == pytest.approx(-15, abs=1e-3)      # X right
    far = {"section": {"origin": [0, 500, 0], "normal": [0, 1, 0]}}
    assert section(placed, far) == []
    top = {"section": {"origin": [0, 0, 10], "normal": [0, 0, 1]}}               # horizontal cut: wall ring
    ring = {c.name: c for c in section(placed, top)}
    assert ring["box"].area_mm2 == pytest.approx(30 * 20 - 26 * 16, rel=1e-3)


def test_cli_fit_writes_the_section_png(tmp_path, monkeypatch, capsys):
    from fsgen.cli import main
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["studio", "fit", str(BOX)])
    assert e.value.code == 0
    png = tmp_path / "out/lidded_box/lidded_box_section.png"
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and png.stat().st_size > 5000
    assert "_section.png" in capsys.readouterr().out


def test_explode_spreads_parts_in_order_along_the_direction(placed):
    items = explode(placed, {"view": {"explode": [0, 0, 1], "explode_distance": 10}})
    by_name = {n: G.bbox([s])[0][2] for n, s, _ in items}
    base = {p.name: G.bbox([p.shape])[0][2] for p in placed}
    order = sorted(base, key=lambda n: G.props(next(p.shape for p in placed if p.name == n), "volume")
                   .CentreOfMass().Z())
    assert [round(by_name[n] - base[n]) for n in order] == [0, 10, 20]


def test_viewer_has_an_exploded_scene(tmp_path, monkeypatch):
    from fsgen.cli import main
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["studio", "view", str(BOX)])
    assert e.value.code == 0
    page = (tmp_path / "out/lidded_box/lidded_box_view.html").read_text()
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.S)
                      .group(1).replace("<\\/", "</"))
    assert [s["name"] for s in data["scenes"]] == ["As printed", "Assembled", "Exploded"]
    assert sorted(p["label"] for p in data["scenes"][2]["parts"]) == ["box", "card", "lid"]
    assert base64.b64decode(data["scenes"][2]["parts"][0]["stl"])[:80]


REVOLVED = """FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");
export function build(context is Context, id is Id)
{
    var sk = newSketch(context, id + "Profile", { "sketchPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) });
    skArc(sk, "a", { "start" : vector(0, 0) * millimeter, "mid" : vector(10, 0.0417) * millimeter, "end" : vector(20, 0.1667) * millimeter });
    skPolyline(sk, "p", { "points" : [vector(20, 0.1667) * millimeter, vector(20, 5) * millimeter,
                vector(10, 5) * millimeter, vector(10, 8) * millimeter, vector(0, 8) * millimeter, vector(0, 0) * millimeter] });
    skLineSegment(sk, "axis", { "start" : vector(0, 0) * millimeter, "end" : vector(0, 1) * millimeter, "construction" : true });
    skSolve(sk);
    revolve(context, id + "Disc", { "entities" : qSketchRegion(id + "Profile"),
        "axis" : sketchEntityQuery(id + "Profile", EntityType.EDGE, "axis"), "fullRevolve" : true });
    var h = newSketch(context, id + "Hole", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skCircle(h, "c", { "center" : vector(-12, 0) * millimeter, "radius" : 2 * millimeter });
    skSolve(h);
    extrude(context, id + "Hole cut", { "entities" : qSketchRegion(id + "Hole"), "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.THROUGH_ALL, "defaultScope" : false, "booleanScope" : qCreatedBy(id + "Disc", EntityType.BODY) });
}
"""


def test_section_cuts_a_revolved_part_through_its_seam_plane():
    """Revolved from the Front plane (seam in y = 0) with a curved base and a hole on the cut: a 0.01 mm slab cut
    returned slivers and dropped the part."""
    loc = build_local(trace(REVOLVED))
    spec = {"parts": {"disc": {"at": [5, 0, 2], "place": [{"rotate": 180, "axis": [1, 0, 0]}, {"move": [0, 0, 8]}]}}}
    placed = place([b.shape for b in loc.run.solids], spec, ROOT)
    cut = {c.name: c for c in section(placed, spec)}
    assert cut["disc"].area_mm2 == pytest.approx(237.9, abs=0.5)     # 40 x 5 + 20 x 3, less arc sag and the hole


def test_cli_build_refuses_to_export_invalid_geometry(tmp_path, monkeypatch, capsys):
    from fsgen.cli import main
    from fsgen.native import geomcheck
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(geomcheck, "validity", lambda solids: [geomcheck.Validity(1, ["invalid B-rep (BRepCheck)"], 1)])
    with pytest.raises(SystemExit) as e:
        main(["studio", "build", str(BOX)])
    assert e.value.code == 1
    assert "not exporting" in capsys.readouterr().out
    assert not (tmp_path / "out/lidded_box").exists()
