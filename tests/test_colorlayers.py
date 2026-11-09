"""colorlayers: disjoint split, thin-band widening, SVG import, placement (pure shapely, no CAD kernel)."""
import pytest
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from scriptforge.native import colorlayers as cl
from scriptforge.native.colorlayers import Layer


def L(name, g, h=0.5, color=None):
    return Layer(color or {"A": "#ff0000", "B": "#00ff00", "C": "#0000ff"}.get(name, "#808080"), name, g, h)


def test_split_makes_layers_disjoint_and_drops_specks():
    dropped = []
    top = L("A", box(0, 0, 10, 10))
    mid = L("B", box(5, 5, 15, 15))
    low = L("C", unary_union([box(0, 0, 20, 20), box(30, 0, 30.09, 0.09)]))     # + a speck far away
    out = cl.split([top, mid, low], dropped)
    gs = [x.geometry for x in out]
    for i in range(3):
        for j in range(i + 1, 3):
            assert gs[i].intersection(gs[j]).area < 1e-6
    assert gs[0].area == pytest.approx(100)
    assert gs[1].area == pytest.approx(100 - 25, abs=0.1)
    assert unary_union(gs).area == pytest.approx(400, abs=0.5)               # the union of all layers is unchanged
    assert any(a < cl.MIN_AREA for _, a in dropped)                       # the speck was dropped and recorded


def test_widen_grows_a_thin_band_and_stays_disjoint():
    band = box(0, 0, 10, 0.2)
    a = L("A", band)
    b = L("B", box(-5, -5, 15, 5))               # lower layer under and around the band
    out, left = cl.widen(cl.split([a, b]), 0.45)
    assert left == []
    assert cl.narrowest(out[0].geometry) >= 0.43
    assert out[0].geometry.area > band.area * 2
    assert out[0].geometry.intersection(out[1].geometry).area < 1e-6
    assert unary_union([x.geometry for x in out]).area == pytest.approx(200, abs=0.5)  # nothing lost or invented


def test_widen_never_eats_higher_layers_and_ignores_tips():
    hi = L("A", box(0, 0.2, 10, 3))              # sits right above the band: it must not lose anything
    band = L("B", box(0, 0, 10, 0.2))            # 0.2 mm band, blocked on one side
    tip = L("C", Point(20, 0).buffer(1.0).intersection(box(20, -1, 25, 1)))   # a half disc: no band
    star_tip = L("C", Polygon([(30, 0), (30.8, 0.1), (31, 0.2)]).buffer(0.0))  # tiny spike: area << 0.5 w^2
    ls = cl.split([hi, band, tip])
    hi_before = ls[0].geometry.area
    out, left = cl.widen(ls, 0.45)
    assert out[0].geometry.area == pytest.approx(hi_before)
    assert out[1].geometry.bounds[1] < -0.2                                   # it grew away from the blocked side
    assert out[1].geometry.intersection(out[0].geometry).area < 1e-6          # and never over the higher layer
    assert cl.thin_report([out[1]], 0.45) == []
    assert out[2].geometry.symmetric_difference(ls[2].geometry).area < 1e-6    # half disc untouched
    assert cl.thin_report([star_tip], 0.45) == []


def test_thin_report_lists_band_but_not_a_speck():
    ls = [L("A", unary_union([box(0, 0, 10, 0.2), box(20, 0, 20.2, 0.2)]))]
    rep = cl.thin_report(ls, 0.45)
    assert len(rep) == 1 and rep[0].name == "A" and rep[0].width == pytest.approx(0.2, abs=0.03)
    assert rep[0].bbox[2] == pytest.approx(10, abs=0.1)


def test_narrowest():
    assert cl.narrowest(box(0, 0, 10, 0.5)) == pytest.approx(0.5, abs=0.05)
    assert cl.narrowest(box(0, 0, 10, 10)) > 3


def test_fit_and_mirror():
    ls = [L("A", box(0, 0, 4, 2))]
    f = cl.fit(ls, 10, center=(5, 7), mirror_u=False)
    assert f[0].geometry.bounds == pytest.approx((0, 4.5, 10, 9.5), abs=1e-9)
    asym = [L("A", unary_union([box(0, 0, 3, 1), box(0, 1, 1, 2)]))]
    m = cl.fit(asym, 3, mirror_u=True)
    assert m[0].geometry.contains(Point(1.0, 0.5)) and not m[0].geometry.contains(Point(-1.0, 0.5))   # column now on the right
    assert cl.mirror(ls)[0].geometry.bounds == pytest.approx((-4, 0, 0, 2))


SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="20mm" viewBox="0 0 100 100">
  <rect x="0" y="0" width="60" height="60" fill="#ff0000"/>
  <rect x="40" y="40" width="60" height="60" fill="#0000ff"/>
  <g transform="translate(10,10)">
    <path d="M0 0 H20 V20 H0 Z M5 5 V15 H15 V5 Z" fill="#00ff00" fill-rule="evenodd"/>
  </g>
  <circle cx="90" cy="10" r="5" fill="none" stroke="#000"/>
  <text x="1" y="1">hi</text>
</svg>"""


def test_from_svg(tmp_path):
    pytest.importorskip("svgelements")
    p = tmp_path / "a.svg"
    p.write_text(SVG)
    with pytest.warns(UserWarning, match="ignored"):
        ls = cl.from_svg(p, height=0.7, names={"#0000ff": "Blue"})
    by = {x.color: x for x in ls}
    assert set(by) == {"#ff0000", "#0000ff", "#00ff00"}                  # stroke-only circle and text skipped
    assert [x.color for x in ls] == ["#00ff00", "#0000ff", "#ff0000"]    # topmost-shape order: green drawn last
    assert all(x.height == 0.7 for x in ls)
    assert by["#0000ff"].name == "Blue" and by["#ff0000"].name == "Red"
    # viewBox 100 -> 20 mm: 1 unit = 0.2 mm; red 60x60 -> 12x12 minus the blue overlap (20x20 -> 4x4) minus the green frame
    b = unary_union([x.geometry for x in ls]).bounds
    assert b == pytest.approx((0, -20, 20, 0), abs=0.02)                  # +y up: the SVG's top edge is y = 0, bottom -20
    assert by["#0000ff"].geometry.area == pytest.approx(12 * 12, abs=0.1)
    # green: 20 u box with a 10 u hole (even-odd), translate(10,10): 4 mm box with a 2 mm hole
    assert by["#00ff00"].geometry.area == pytest.approx(4 * 4 - 2 * 2, abs=0.05)
    assert by["#00ff00"].geometry.bounds == pytest.approx((2, -6, 6, -2), abs=0.02)   # translate(10,10) applied
    # painter: red lost the overlap with blue and the green frame (the hole shows red through)
    red = by["#ff0000"].geometry
    assert red.area == pytest.approx(12 * 12 - 4 * 4 - 12, abs=0.1)
    assert red.contains(Point(4, -4))                                     # inside the green hole: red is visible
    assert not red.contains(Point(3, -3))                                 # under the green frame
    assert not red.contains(Point(11, -11))                               # under blue
    # fit helper
    f = cl.fit(ls, 30, center=(0, 0))
    assert unary_union([x.geometry for x in f]).bounds == pytest.approx((-15, -15, 15, 15), abs=0.02)


def test_color_name():
    assert cl.color_name("#f5d90a") == "Yellow"
    assert cl.color_name("#101010") == "Black"
    assert cl.color_name("#123456") == "Navy"
    assert cl.color_name("#00ffff") == "#00ffff"                            # nothing near: the hex is kept
    assert cl.color_name("#abcdef", {"#abcdef": "Sky"}) == "Sky"


def test_widen_absorbs_an_enclosed_gap_into_the_neighbour_with_the_longest_border():
    gray = L("A", box(0, 0, 10, 4))
    yellow = L("B", box(0, 4.15, 10, 8))                       # 0.15 mm black gap between gray and yellow
    black = L("C", box(-2, -2, 12, 10))
    ls = cl.split([gray, yellow, black])
    _, left0 = cl.widen(ls, 0.45, absorb=False)
    assert left0 and all(t.name == "C" for t in left0)         # without absorb the gap stays thin
    ab = []
    out, left = cl.widen(ls, 0.45, absorbed=ab)
    assert left == [] and cl.thin_report(out, 0.45) == []
    assert len(ab) == 1 and ab[0][0] == "C" and ab[0][1] in ("A", "B")
    assert out[0].geometry.area + out[1].geometry.area > 40 + 31.4 + 0.4       # the gap went to gray/yellow
    gs = [x.geometry for x in out]
    for i in range(3):
        for j in range(i + 1, 3):
            assert gs[i].intersection(gs[j]).area < 1e-6
    assert unary_union(gs).area == pytest.approx(unary_union([x.geometry for x in ls]).area, abs=0.1)


def test_widen_adds_no_spikes_beyond_the_parent_buffer():
    # letter-like: two blocks joined by a thin neck, with sharp wedge terminals (a mitre growth spikes there)
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union
    parent = unary_union([box(0, 0, 2, 1.5), box(3, 0, 5, 1.5), box(2, 0.6, 3, 0.8),
                          Polygon([(5, 0), (6.5, 0.05), (5, 0.3)]), Polygon([(0, 1.5), (-1.2, 1.55), (0, 1.2)])])
    ly = cl.Layer("#f5d90a", "Yellow", parent, 0.8)
    out, _ = cl.widen([ly], 0.45)
    g = out[0].geometry
    d = 0.45
    assert g.area > parent.area
    assert g.hausdorff_distance(parent) <= d + 0.01
    assert parent.hausdorff_distance(g) <= d + 0.01
    assert g.buffer(0.1).contains(parent)   # nothing removed (sharp tips lose < 0.1 mm to the smoothing)
