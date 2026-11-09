"""Editing script variables: set_variables text rewrite, check with overrides, and PUT /api/variables."""
import http.client
import json
import shutil
import threading
from pathlib import Path

import pytest

from fsgen.native.script import set_variables
from fsgen.web import checkworker, parts_api  # noqa: F401
from fsgen.web import server as srv

H = {"X-Fsgen": "1"}
PLATE = Path(__file__).resolve().parent.parent / "examples" / "native" / "plate.fs"

SRC = '''// variable(context, "a", 1 * mm);
export function build(context is Context, id is Id)
{
    var s = "variable(context, \\"a\\", 9)";
    /* variable(context, "a", 2) */
    variable(context, "a", (3 + 2) * millimeter, "has, commas (and) parens");
    variable( context ,"b" ,  7 * millimeter );
    myvariable(context, "c", 1);
}
'''


def test_set_variables_rewrites_only_value():
    out = set_variables(SRC, {"a": "20 * millimeter", "b": "(1 + 2) * mm"})
    assert out == SRC.replace("(3 + 2) * millimeter", "20 * millimeter").replace("7 * millimeter", "(1 + 2) * mm")
    assert set_variables(SRC, {}) == SRC


def test_set_variables_errors():
    with pytest.raises(ValueError, match="zz"):
        set_variables(SRC, {"zz": "1"})
    with pytest.raises(ValueError, match="c"):
        set_variables(SRC, {"c": "1"})  # only inside an identifier: not a variable call
    dup = SRC + 'variable(context, "b", 1 * mm);\n'
    with pytest.raises(ValueError, match="b"):
        set_variables(dup, {"b": "1"})
    for bad in ("", "  ", "1; evil", 'a"b', "x" * 61, "1\n2", "a,b"):
        with pytest.raises(ValueError, match="a"):
            set_variables(SRC, {"a": bad})


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    root = tmp_path_factory.mktemp("var_root")
    shutil.copy(PLATE, root / "plate.fs")
    httpd, app = srv.make_server(root, "127.0.0.1", 0, "pw")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
    c.request("POST", "/api/login", json.dumps({"password": "pw"}), H)
    r = c.getresponse()
    r.read()
    cookie = r.getheader("Set-Cookie").split(";")[0].split("=", 1)[1]
    yield httpd.server_address[1], root, cookie
    checkworker.shutdown()
    httpd.shutdown()
    httpd.server_close()


def call(web, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", web[0], timeout=120)
    c.request(method, path, json.dumps(body) if body is not None else None, {**H, "Cookie": f"fsgen_session={web[2]}"})
    r = c.getresponse()
    raw = r.read()
    return r.status, json.loads(raw) if raw else None


def test_check_with_overrides_does_not_touch_file(web):
    f = web[1] / "plate.fs"
    before = f.read_text()
    s, base = call(web, "POST", "/api/check", {"path": "plate.fs"})
    assert s == 200 and base["ok"]
    assert {v["name"]: v["expression"] for v in base["variables"]}["width"] == "80 mm"
    s, new = call(web, "POST", "/api/check", {"path": "plate.fs", "variables": {"width": "100 * millimeter"}})
    assert s == 200 and new["ok"]
    assert {v["name"]: v["expression"] for v in new["variables"]}["width"] == "100 mm"
    assert new["scenes"] != base["scenes"]  # geometry changed
    assert f.read_text() == before
    s, d = call(web, "POST", "/api/check", {"path": "plate.fs", "variables": {"width": "1; x"}})
    assert s == 400 and "width" in d["error"]
    s, d = call(web, "POST", "/api/check", {"path": "plate.fs", "variables": {"nope": "1"}})
    assert s == 400 and "nope" in d["error"]
    s, d = call(web, "POST", "/api/check", {"path": "plate.fs", "variables": {"width": "banana"}})
    assert s == 200 and d["error"] and not d["ok"]


def test_put_variables_saves_only_on_success(web):
    f = web[1] / "plate.fs"
    before = f.read_text()
    s, d = call(web, "PUT", "/api/variables", {"path": "plate.fs", "variables": {"width": "banana"}})
    assert s == 422 and d["error"]
    assert f.read_text() == before
    s, d = call(web, "PUT", "/api/variables", {"path": "plate.fs", "variables": {"nope": "1"}})
    assert s == 400 and f.read_text() == before
    s, d = call(web, "PUT", "/api/variables", {"path": "plate.fs", "variables": {"width": "100 * millimeter"}})
    assert s == 200 and d["ok"] and d["mtime"] > 0
    assert f.read_text() == before.replace("80 * millimeter", "100 * millimeter")


def test_variables_path_validation(web):
    for path in ("../etc/passwd", "/etc/passwd", "sub/../../x.fs"):
        s, _ = call(web, "PUT", "/api/variables", {"path": path, "variables": {"a": "1"}})
        assert s in (400, 403, 404), path
    assert call(web, "PUT", "/api/variables", {"path": "missing.fs", "variables": {"a": "1"}})[0] == 404
    assert call(web, "PUT", "/api/variables", {"path": "plate.fs"})[0] == 400


def test_set_variables_accepts_the_reported_unit_form():
    from fsgen.native.script import set_variables, trace
    src = 'FeatureScript 2384;\nimport(path : "onshape/std/geometry.fs", version : "2384.0");\n' \
          'annotation { "Feature Type Name" : "P" }\nexport const p = defineFeature(function(context is Context, id is Id, definition is map)\n' \
          'precondition {}\n{\n    variable(context, "w", 80 * millimeter);\n    variable(context, "a", 30 * degree);\n});\n'
    out = set_variables(src, {"w": "10 mm", "a": "45 deg"})
    assert 'variable(context, "w", 10 * millimeter)' in out and 'variable(context, "a", 45 * degree)' in out
    assert set_variables(src, {"w": "(2 + 3) * millimeter"}).count("(2 + 3) * millimeter") == 1
    from pathlib import Path
    plate = (Path(__file__).resolve().parents[1] / "examples/native/plate.fs").read_text()
    tr = trace(set_variables(plate, {"thickness": "12.5 mm"}))
    assert tr.ok, tr.error
    assert {v["name"]: v["expression"] for v in tr.variable_list}["thickness"] == "12.5 mm"
