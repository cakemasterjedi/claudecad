"""emboss: FeatureScript for raised multicolor art, built locally (fslite) on a box and on a cylinder."""
import re
import zipfile

import pytest
from shapely.geometry import box

from fsgen.cli import main
from fsgen.fslite import geom as G
from fsgen.native import colorlayers as cl
from fsgen.native.colorlayers import Layer
from fsgen.native.emboss import Emboss
from fsgen.native.local import build_local
from fsgen.native.script import trace

HEAD = 'FeatureScript 3083;\nimport(path : "onshape/std/common.fs", version : "3083.0");\n'


def vol(b):
    return G.props(b.shape, "volume").Mass() * 1e9


def build(src):
    loc = build_local(trace(src))
    assert loc.ok, (loc.error, loc.warnings)
    return loc


def layers():
    """Gold 0.8 mm (with a hole) above Navy 0.4 mm, split to be disjoint."""
    gold = Layer("#d4a017", "Gold", box(-6, -6, 0, 6).difference(box(-4, -2, -2, 2)), 0.8)
    navy = Layer("#0b1f4d", "Navy", box(-8, -8, 8, 8), 0.4)
    return cl.split([gold, navy])


def flat_script(tmp_layers):
    em = Emboss(base="qCreatedBy(id + \"Base\", EntityType.BODY)")
    # the plane sits 0.2 mm INSIDE the box top (z = 4.8), so the base subtraction matters
    plane = 'qCreatedBy(id + "Art plane", EntityType.FACE)'
    em.flat("Art", plane, tmp_layers)
    em.properties(base=("Base", "#444444"))
    src = (HEAD + "export function build(context is Context, id is Id)\n{\n"
           '    var s = newSketch(context, id + "Base sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });\n'
           '    skRectangle(s, "r", { "firstCorner" : vector(-10, -10) * millimeter, "secondCorner" : vector(10, 10) * millimeter });\n'
           '    skSolve(s);\n'
           '    extrude(context, id + "Base", { "entities" : qSketchRegion(id + "Base sketch"), "endBound" : BoundingType.BLIND, "depth" : 5 * millimeter });\n'
           '    cPlane(context, id + "Art plane", { "entities" : qCreatedBy(makeId("Top"), EntityType.FACE), "offset" : 4.8 * millimeter });\n'
           + em.source() + "}\n")
    return em, src


def test_flat_emboss_bodies_names_and_disjoint():
    em, src = flat_script(layers())
    loc = build(src)
    sol = loc.run.solids
    names = sorted(b.name for b in sol)
    assert names.count("Base") == 1 and names.count("Gold") == 1 and names.count("Navy") == 2   # one body per region (navy fills the hole too)
    by = {b.name: b for b in sol}
    assert by["Gold"].color == pytest.approx((0xd4 / 255, 0xa0 / 255, 0x17 / 255, 1))
    ls = layers()
    # sunk 0.2 mm into the base: only the part above z = 5 remains (0.6 mm gold, 0.2 mm navy)
    assert vol(by["Gold"]) == pytest.approx(ls[0].geometry.area * 0.6, rel=0.01)
    assert sum(vol(b) for b in sol if b.name == "Navy") == pytest.approx(ls[1].geometry.area * 0.2, rel=0.01)
    assert vol(by["Base"]) == pytest.approx(20 * 20 * 5, rel=1e-4)
    sh, _ = G.fuse_all([b.shape for b in sol])                     # disjoint: parts sum to the fused union
    assert sum(vol(b) for b in sol) == pytest.approx(G.props(sh, "volume").Mass() * 1e9, abs=0.05)
    assert em.regions["Gold"] and set(em.groups) == {"Gold", "Navy"}


def test_flat_island_in_a_hole_gets_its_own_sketch():
    ring = box(-6, -6, 6, 6).difference(box(-4, -4, 4, 4))
    lay = [Layer("#ff0000", "Red", ring.union(box(-1, -1, 1, 1)), 0.5)]    # square island inside the hole
    em, src = flat_script(lay)
    assert "inner 1" in src
    loc = build(src)
    assert sum(1 for b in loc.run.solids if b.name == "Red") == 2


R, STANDOFF, DEPTH_IN = 20.0, 1.0, 1.0


def cyl_script(lay):
    em = Emboss(base="qCreatedBy(id + \"Barrel\", EntityType.BODY)")
    em.cylinder("Side", 'qCreatedBy(id + "Side plane", EntityType.FACE)', lay, standoff=STANDOFF, depth_in=DEPTH_IN,
                opposite=True, axis_plane='qCreatedBy(makeId("Top"), EntityType.FACE)', axis_center=(0, 0),
                radius=R, span=30, span_opposite=False)
    em.properties(base=("Barrel", "#444444"))
    src = (HEAD + "export function build(context is Context, id is Id)\n{\n"
           '    var s = newSketch(context, id + "Barrel sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });\n'
           f'    skCircle(s, "c", {{ "center" : vector(0, 0) * millimeter, "radius" : {R} * millimeter }});\n'
           '    skSolve(s);\n'
           '    extrude(context, id + "Barrel", { "entities" : qSketchRegion(id + "Barrel sketch"), "endBound" : BoundingType.BLIND, "depth" : 30 * millimeter });\n'
           f'    cPlane(context, id + "Side plane", {{ "entities" : qCreatedBy(makeId("Right"), EntityType.FACE), "offset" : {R + STANDOFF} * millimeter }});\n'
           + em.source() + "}\n")
    return em, src


def test_cylinder_emboss_follows_the_curve_and_is_disjoint():
    lay = cl.split([Layer("#d4a017", "Gold", box(-4, 8, 4, 22), 0.8), Layer("#0b1f4d", "Navy", box(-6, 6, 6, 24), 0.4)])
    em, src = cyl_script(lay)
    loc = build(src)
    sol = loc.run.solids
    by = {}
    for b in sol:
        by.setdefault(b.name, []).append(b)
    assert {k: len(v) for k, v in by.items()} == {"Barrel": 1, "Gold": 1, "Navy": 1}
    # outer surface: apex at x = R + height, only a thin skin outside the barrel
    import build123d as bd
    from fsgen.native.meshing import to_mm
    gx = bd.Compound(to_mm(by["Gold"][0].shape)).bounding_box()
    nx = bd.Compound(to_mm(by["Navy"][0].shape)).bounding_box()
    assert gx.max.X == pytest.approx(R + 0.8, abs=0.01) and gx.min.X == pytest.approx((R * R - 16) ** 0.5, abs=0.01)   # the barrel surface at y = +-4
    assert nx.max.X == pytest.approx(R + 0.4, abs=0.01)
    # volume ~ footprint area x height (a little more: the sides are radial, the skin is curved)
    assert vol(by["Gold"][0]) == pytest.approx(lay[0].geometry.area * 0.8, rel=0.04)
    assert vol(by["Navy"][0]) == pytest.approx(lay[1].geometry.area * 0.4, rel=0.04)
    sh, _ = G.fuse_all([b.shape for b in sol])
    assert sum(vol(b) for b in sol) == pytest.approx(G.props(sh, "volume").Mass() * 1e9, abs=0.05)


def test_color_group_with_two_colors_is_rejected():
    em = Emboss(base="x")
    em._prisms("A", "p", Layer("#ff0000", "Red", box(0, 0, 1, 1), 1), "1 * millimeter", False)
    with pytest.raises(ValueError):
        em._prisms("B", "p", Layer("#00ff00", "Red", box(0, 0, 1, 1), 1), "1 * millimeter", False)


def test_build_exports_one_3mf_object_per_color(tmp_path, monkeypatch):
    lay = cl.split([Layer("#d4a017", "Gold", box(-4, 8, 4, 22), 0.8), Layer("#0b1f4d", "Navy", box(-6, 6, 6, 24), 0.4)])
    _, src = cyl_script(lay)
    p = tmp_path / "cyl.fs"
    p.write_text(src)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["studio", "build", str(p), "--formats", "step,stl,3mf"])
    assert e.value.code == 0
    with zipfile.ZipFile(tmp_path / "out" / "cyl" / "cyl.3mf") as z:
        xml = z.read("3D/3dmodel.model").decode()
    assert sorted(re.findall(r'<object [^>]*name="(\w+)"', xml)) == ["Barrel", "Gold", "Navy"]
    assert "d4a017" in xml.lower() and "0b1f4d" in xml.lower()
