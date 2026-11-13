from scriptforge.fslite.runner import _drawing_title, drawing_units


def test_inches_detected():
    assert drawing_units("3.5 inches wide by 2 inch tall license plate") == "in"
    assert drawing_units('a 4" hook') == "in"
    assert drawing_units("bracket 2 in wide") == "in"


def test_metric_and_default():
    assert drawing_units("50 mm cube") == "mm"
    assert drawing_units("a box 5cm tall") == "mm"
    assert drawing_units("a phone stand") == "mm"
    assert drawing_units("fits in a drawer, 40mm wide") == "mm"  # 'in' as a word is not a unit


def test_first_unit_wins_when_mixed():
    assert drawing_units("3 inch plate with 5 mm holes") == "in"
    assert drawing_units("5 mm holes on a 3 inch plate") == "mm"


def test_title_keeps_every_line_and_caps_length():
    t = _drawing_title("plate that says love\n\nChange requested afterwards: cleaner lettering")
    assert t.splitlines() == ["plate that says love", "Change requested afterwards: cleaner lettering"]
    assert _drawing_title("\n".join(f"line {i}" for i in range(6))).endswith("…")


def test_tick_step_scales_with_part_size():
    from scriptforge.fslite.runner import tick_step
    assert tick_step(8, "mm") == 1        # tiny part
    assert tick_step(93, "mm") == 20      # license plate
    assert tick_step(400, "mm") == 50    # large part
    assert tick_step(3.7, "in") == 0.5
    assert tick_step(1.2, "in") == 0.25
    assert tick_step(30, "in") == 6
    for ext in (0.5, 3, 37, 180, 900):
        assert ext / tick_step(ext, "mm") <= 8


def test_tick_label_decimals():
    from scriptforge.fslite.runner import tick_label
    assert tick_label(0.25, 0.25) == "0.25"
    assert tick_label(1.0, 0.25) == "1"
    assert tick_label(1.5, 0.25) == "1.5"
    assert tick_label(20.0, 10) == "20"
    assert tick_label(-0.0001, 0.5) == "0"
    assert tick_label(0.125, 0.125) == "0.125"


def _block_with_holes():
    """20x20x6 mm block (meters): two through holes d=4 / d=2.5 along Z, a d=6 boss on top, one side hole along X."""
    from types import SimpleNamespace
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    def cyl(x, y, z, d, h, axis=(0, 0, 1)):
        return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(*axis)), d / 2, h).Shape()

    s = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 0.02, 0.02, 0.006).Shape()
    s = BRepAlgoAPI_Fuse(s, cyl(0.014, 0.014, 0.006, 0.006, 0.004)).Shape()  # boss
    s = BRepAlgoAPI_Cut(s, cyl(0.005, 0.005, -0.001, 0.004, 0.02)).Shape()
    s = BRepAlgoAPI_Cut(s, cyl(0.005, 0.015, -0.001, 0.0025, 0.02)).Shape()
    s = BRepAlgoAPI_Cut(s, cyl(-0.001, 0.010, 0.003, 0.002, 0.03, (1, 0, 0))).Shape()
    return SimpleNamespace(shape=s, color=None, kind="solid")


def test_hole_features_finds_holes_not_bosses():
    from scriptforge.fslite.runner import hole_features
    holes = hole_features([_block_with_holes()])
    dia = sorted(round(d * 1000, 3) for _c, _a, d in holes)
    assert dia == [2.0, 2.5, 4.0]  # the 6 mm boss is convex: not listed
    by_d = {round(d * 1000, 1): (c, a) for c, a, d in holes}
    c, a = by_d[4.0]
    assert abs(a[2]) > 0.99 and abs(c[0] - 0.005) < 1e-6 and abs(c[1] - 0.005) < 1e-6
    assert abs(by_d[2.0][1][0]) > 0.99


def test_hole_labels_filter_axis_group_and_units():
    from scriptforge.fslite.runner import hole_labels
    holes = [((0, 0, 0), (0, 0, 1), 0.005), ((1, 0, 0), (0, 0, -1), 0.0052), ((0, 0, 0), (1, 0, 0), 0.003)]
    assert [t for _c, t in hole_labels(holes, 1000, "mm", 2)] == ["⌀5", "⌀5.2"]
    assert [t for _c, t in hole_labels(holes, 1000, "mm", 0)] == ["⌀3"]
    assert hole_labels(holes, 1000, "mm", 1) == []
    assert hole_labels(holes[:1], 1000 / 25.4, "in", 2)[0][1] == "⌀0.2"
    many = [((i, 0, 0), (0, 0, 1), 0.003) for i in range(13)]
    assert [t for _c, t in hole_labels(many, 1000, "mm", 2)] == ["⌀3 x13"]


def test_render_png_with_holes_and_dimensions(tmp_path):
    from types import SimpleNamespace
    from scriptforge.fslite.runner import render_png
    b = _block_with_holes()
    out = render_png(SimpleNamespace(solids=[b]), tmp_path / "p.png", "block", "mm")
    assert out and out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
