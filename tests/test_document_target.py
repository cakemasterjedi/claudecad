"""--document targeting and feature-pattern defaults (offline, fake Onshape)."""
from pathlib import Path

import pytest

from fsgen.cloud import parse_document
from fsgen.costmodel import fake_onshape
from fsgen.native import build as nb
from fsgen.native.script import trace
from fsgen.onshape import Onshape

PLATE = (Path(__file__).resolve().parent.parent / "examples/native/plate.fs").read_text()
DOC_A = "https://cad.onshape.com/documents/" + "a" * 24 + "/w/" + "1" * 24 + "/e/" + "f" * 24
DOC_B = "https://cad.onshape.com/documents/" + "a" * 24 + "/w/" + "2" * 24  # a branch of the same document


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONSHAPE_ACCESS_KEY", "a")
    monkeypatch.setenv("ONSHAPE_SECRET_KEY", "b")
    monkeypatch.delenv("FSGEN_DOCUMENT", raising=False)


def test_parse_document_urls():
    assert parse_document(DOC_A) == {"did": "a" * 24, "wid": "1" * 24}
    with pytest.raises(ValueError):
        parse_document("https://cad.onshape.com/documents/abc")


def test_same_part_name_in_two_workspaces_keeps_separate_state():
    from fsgen.cloud import target_workspace
    with fake_onshape():
        o = Onshape()
        for url in (DOC_A, DOC_B):
            ws = target_workspace(o, url)
            st = nb.get_studio(o, "Plate", ws["did"], ws["wid"])
            assert nb.push_trace(o, st, trace(PLATE), log=lambda s: None).ok
        assert o.calls == 2 * (len(trace(PLATE).features) + 1 + 3)  # no document creation calls
    a = nb.find_state("Plate", "a" * 24, "1" * 24)
    b = nb.find_state("Plate", "a" * 24, "2" * 24)
    assert a and b and a.eid != b.eid
    with fake_onshape():  # re-push to the branch: nothing changed, 0 calls
        o = Onshape()
        assert nb.push_trace(o, nb.get_studio(o, "Plate", "a" * 24, "2" * 24), trace(PLATE), log=lambda s: None).ok
        assert o.calls == 0


def test_legacy_state_keyed_by_name_is_still_found(tmp_path):
    st = nb.StudioState("d" * 24, "w" * 24, "e" * 24, "Old part")  # written before per-document keys
    nb.save_state(st)
    assert nb.find_state("Old part", "d" * 24, "w" * 24).eid == "e" * 24
    assert nb.find_state("Old part", "d" * 24, "x" * 24) is None  # other workspace: a new studio


def test_feature_patterns_reapply_features_by_default():
    tr = trace(PLATE)
    for f in tr.features:
        if f.feature_type == "mirror":
            params = {p["parameterId"]: p for p in nb.feature_json(f, {g.name: "F" for g in tr.features})["parameters"]}
            assert params["fullFeaturePattern"]["value"] is True
    explicit = trace(PLATE.replace('"patternType" : PatternType.FEATURE,',
                                   '"patternType" : PatternType.FEATURE, "fullFeaturePattern" : false,', 1))
    m = next(f for f in explicit.features if f.feature_type == "mirror")
    params = {p["parameterId"]: p for p in nb.feature_json(m, {g.name: "F" for g in explicit.features})["parameters"]}
    assert params["fullFeaturePattern"]["value"] is False  # an explicit choice in the script wins


class _DocsApi:
    """Records POST payloads; refuses private documents like a free Onshape plan."""
    def __init__(self, free=False):
        self.free, self.posts = free, []

    def post(self, path, body):
        from fsgen.onshape import OnshapeError
        self.posts.append((path, body))
        if path == "documents":
            if self.free and not body["isPublic"]:
                raise OnshapeError('POST documents -> 409: {"message" : "Free accounts only allow access to public '
                                   'documents. Upgrade your account to get full access to private documents."}')
            return {"id": "d1", "defaultWorkspace": {"id": "w1"}}
        return {"id": path.split("/")[0]}

    def get(self, path):
        return [{"elementType": "PARTSTUDIO", "id": "p1"}]


def test_new_document_is_private_unless_public_is_asked(tmp_path, monkeypatch):
    from fsgen.cloud import ensure_workspace
    monkeypatch.delenv("FSGEN_PUBLIC", raising=False)
    api = _DocsApi()
    ensure_workspace(api, state_file=tmp_path / "a.json")
    ensure_workspace(api, state_file=tmp_path / "b.json", public=True)
    monkeypatch.setenv("FSGEN_PUBLIC", "1")
    ensure_workspace(api, state_file=tmp_path / "c.json")
    assert [b["isPublic"] for p, b in api.posts if p == "documents"] == [False, True, True]


def test_free_plan_refusal_says_how_to_push(tmp_path, monkeypatch):
    import pytest
    from fsgen.cloud import ensure_workspace
    from fsgen.onshape import OnshapeError
    monkeypatch.delenv("FSGEN_PUBLIC", raising=False)
    with pytest.raises(OnshapeError, match="--public"):
        ensure_workspace(_DocsApi(free=True), state_file=tmp_path / "s.json")
    assert not (tmp_path / "s.json").exists()


def _arc_script(mid_y):
    return f"""FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");
export function build(context is Context, id is Id)
{{
    var sk = newSketch(context, id + "S", {{ "sketchPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) }});
    skArc(sk, "a", {{ "start" : vector(26.3, 1.463) * millimeter, "mid" : vector(29.9, {mid_y}) * millimeter,
                     "end" : vector(33.5, 1.253) * millimeter }});
    skSolve(sk);
}}
"""


def test_nearly_straight_arc_is_refused_with_a_hint():
    from fsgen.native.script import trace
    flat = trace(_arc_script(1.364))           # 0.006 mm off the chord: Onshape flipped this one
    assert not flat.ok and "nearly straight" in flat.error and "skLineSegment" in flat.error
    assert trace(_arc_script(1.5)).ok          # clearly curved
