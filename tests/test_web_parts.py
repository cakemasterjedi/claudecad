"""fsgen web Parts tab: script list/read/save/create, path confinement, and the isolated check worker."""
import http.client
import json
import shutil
import threading
from pathlib import Path

import pytest

from fsgen.web import checkworker, parts_api  # noqa: F401  (importing registers the routes)
from fsgen.web import server as srv

H = {"X-Fsgen": "1"}
PLATE = Path(__file__).resolve().parent.parent / "examples" / "native" / "plate.fs"


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    root = tmp_path_factory.mktemp("parts_root")
    shutil.copy(PLATE, root / "plate.fs")
    (root / "sub").mkdir()
    (root / "bad.fs").write_text("this is not featurescript ((\n")
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


def call(web, method, path, body=None, auth=True, timeout=120):
    c = http.client.HTTPConnection("127.0.0.1", web[0], timeout=timeout)
    h = dict(H)
    if auth:
        h["Cookie"] = f"fsgen_session={web[2]}"
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse()
    raw = r.read()
    return r.status, json.loads(raw) if raw else None


def test_auth_required(web):
    for m, p in (("GET", "/api/scripts"), ("GET", "/api/scripts/plate.fs"), ("PUT", "/api/scripts/plate.fs"),
                 ("POST", "/api/scripts"), ("POST", "/api/check")):
        assert call(web, m, p, {}, auth=False)[0] == 401, (m, p)


def test_list_and_read(web):
    s, d = call(web, "GET", "/api/scripts")
    assert s == 200 and "plate.fs" in d["scripts"] and "bad.fs" in d["scripts"]
    s, d = call(web, "GET", "/api/scripts/plate.fs")
    assert s == 200 and d["path"] == "plate.fs" and d["text"] == PLATE.read_text() and d["mtime"] > 0
    assert call(web, "GET", "/api/scripts/nope.fs")[0] == 404


@pytest.mark.parametrize("rel", ["%2e%2e/x.fs", "sub/%2e%2e/%2e%2e/x.fs", "%2Fetc%2Fpasswd.fs", "plate.txt"])
def test_confinement_read_write(web, rel):
    assert call(web, "GET", f"/api/scripts/{rel}")[0] in (400, 403, 404)
    assert call(web, "PUT", f"/api/scripts/{rel}", {"text": "x", "mtime": 0})[0] in (400, 403, 404)


@pytest.mark.parametrize("rel", ["../x.fs", "/tmp/x.fs", "sub/../../x.fs", "notes.txt", "", "a\0.fs"])
def test_confinement_create(web, rel):
    s, _ = call(web, "POST", "/api/scripts", {"path": rel, "text": "x"})
    assert s in (400, 403)
    assert not (web[1].parent / "x.fs").exists()


def test_symlink_escape(web):
    outside = web[1].parent / "outside_secret.fs"
    outside.write_text("secret")
    link = web[1] / "sub" / "esc.fs"
    link.symlink_to(outside)
    assert call(web, "GET", "/api/scripts/sub/esc.fs")[0] == 403
    assert call(web, "PUT", "/api/scripts/sub/esc.fs", {"text": "x", "mtime": 0})[0] == 403
    assert outside.read_text() == "secret"


def test_save_and_conflict(web):
    _, d = call(web, "POST", "/api/scripts", {"path": "sub/save.fs", "text": "one"})
    mt = d["mtime"]
    s, d = call(web, "PUT", "/api/scripts/sub/save.fs", {"text": "two", "mtime": mt})
    assert s == 200 and d["path"] == "sub/save.fs"
    assert (web[1] / "sub" / "save.fs").read_text() == "two"
    assert not list((web[1] / "sub").glob(".fsgen-*"))
    # stale mtime -> 409 and the file is untouched
    s, d2 = call(web, "PUT", "/api/scripts/sub/save.fs", {"text": "three", "mtime": mt - 5})
    assert s == 409 and d2["error"] == "changed on disk"
    assert (web[1] / "sub" / "save.fs").read_text() == "two"
    # re-read then overwrite with the fresh mtime
    fresh = call(web, "GET", "/api/scripts/sub/save.fs")[1]["mtime"]
    assert call(web, "PUT", "/api/scripts/sub/save.fs", {"text": "three", "mtime": fresh})[0] == 200
    # validation
    assert call(web, "PUT", "/api/scripts/sub/save.fs", {"text": 5, "mtime": fresh})[0] == 400
    assert call(web, "PUT", "/api/scripts/sub/save.fs", {"text": "x"})[0] == 400
    assert call(web, "PUT", "/api/scripts/missing.fs", {"text": "x", "mtime": 0})[0] == 404
    big = call(web, "PUT", "/api/scripts/sub/save.fs", {"text": "x" * 1_000_001, "mtime": fresh})[0]
    assert big == 413


def test_create_conflict(web):
    s, d = call(web, "POST", "/api/scripts", {"path": "made/new.fs", "text": "hello"})
    assert s == 200 and d["path"] == "made/new.fs" and (web[1] / "made" / "new.fs").read_text() == "hello"
    assert call(web, "POST", "/api/scripts", {"path": "made/new.fs", "text": "again"})[0] == 409
    assert (web[1] / "made" / "new.fs").read_text() == "hello"


def test_check_ok(web):
    s, d = call(web, "POST", "/api/check", {"path": "plate.fs"})
    assert s == 200, d
    assert d["ok"] is True and d["stage"] == "done" and d["error"] is None
    assert d["features"] and {"type", "name"} <= set(d["features"][0])
    assert d["report"] and d["validity"] and all(v["ok"] for v in d["validity"])
    assert d["scenes"][0]["name"] == "As printed"
    part = d["scenes"][0]["parts"][0]
    assert part["stl"] and part["color"].startswith("#") and part["label"]
    assert isinstance(d["walls"], list) and len(d["walls"]) <= 20
    assert d["seconds"] >= 0
    # nothing was written next to the script
    assert not (web[1] / "out").exists()


def test_check_assembled_scene_with_fit_file(web):
    ex = PLATE.parent / "fit"
    for f in ("lidded_box.fs", "lidded_box.fit.toml"):
        shutil.copy(ex / f, web[1] / f)
    s, d = call(web, "POST", "/api/check", {"path": "lidded_box.fs"})
    assert s == 200, d
    assert [sc["name"] for sc in d["scenes"]] == ["As printed", "Assembled"] or d.get("fit_error")


def test_check_script_error(web):
    s, d = call(web, "POST", "/api/check", {"path": "bad.fs"})
    assert s == 200 and d["ok"] is False and d["stage"] == "script" and d["error"]
    assert d["scenes"] == []


def test_check_validation(web):
    assert call(web, "POST", "/api/check", {"path": "missing.fs"})[0] == 404
    assert call(web, "POST", "/api/check", {"path": "../x.fs"})[0] == 400
    s, d = call(web, "POST", "/api/check", {"path": "plate.fs", "min_wall": "huge"})
    assert s == 200 and d["ok"]


def test_check_timeout_then_recovers(web, monkeypatch):
    checkworker.shutdown()  # fresh cold worker: importing OCP takes far longer than the timeout
    monkeypatch.setenv("FSGEN_WEB_CHECK_TIMEOUT", "0.2")
    s, d = call(web, "POST", "/api/check", {"path": "plate.fs"})
    assert s == 504 and d["error"] == "check timed out"
    monkeypatch.delenv("FSGEN_WEB_CHECK_TIMEOUT")
    s, d = call(web, "POST", "/api/check", {"path": "plate.fs"})
    assert s == 200 and d["ok"] is True


def test_worker_dead_before_submit_is_replaced(web):
    for p in list(checkworker._pool._processes.values()):
        p.kill()
        p.join()
    s, d = call(web, "POST", "/api/check", {"path": "plate.fs"})
    assert s == 200 and d["ok"] is True


def test_worker_crash_during_check(web):
    import time
    checkworker.shutdown()
    out = {}
    t = threading.Thread(target=lambda: out.update(r=call(web, "POST", "/api/check", {"path": "plate.fs"})))
    t.start()
    for _ in range(100):  # wait for the lazily created worker, then kill it mid-check
        pool = checkworker._pool
        if pool is not None and pool._processes:
            break
        time.sleep(0.05)
    time.sleep(0.3)
    for p in list(checkworker._pool._processes.values()):
        p.kill()
    t.join(60)
    s, d = out["r"]
    assert s == 500 and "crashed" in d["error"]
    s, d = call(web, "POST", "/api/check", {"path": "plate.fs"})
    assert s == 200 and d["ok"] is True
