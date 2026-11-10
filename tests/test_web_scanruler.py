"""ScanRuler hosting: /scanruler/ static serving, raw scan uploads, check.scanruler archives (no network)."""
import http.client
import io
import json
import threading
import zipfile

import pytest

import scriptforge.web.generate_api as ga
import scriptforge.web.scanruler_api as sa
from scriptforge.web import server as srv

H = {"X-ScriptForge": "1"}
PLY = b"ply\nformat ascii 1.0\nend_header\n"
STL = b"solid x\nendsolid x\n"


@pytest.fixture
def web(tmp_path, monkeypatch):
    home = tmp_path / "sr"
    dist = home / "v0.11.1" / "dist-sf"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<script src="/scanruler/assets/app-1.js"></script>')
    (dist / "assets/app-1.js").write_text("1")
    (dist / "assets/a.wasm").write_bytes(b"\0asm\1\0\0\0")
    (dist / "assets/meshWorker-abc.js").write_text("w")
    (home / "current").symlink_to("v0.11.1/dist-sf")
    monkeypatch.setenv("SCANRULER_HOME", str(home))
    ga._JOBS.clear()
    root = tmp_path / "root"
    root.mkdir()
    httpd, app = srv.make_server(root, "127.0.0.1", 0, "pw")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    app.port = httpd.server_address[1]
    app._tok = app.sessions.create()
    app.home = home
    yield app
    httpd.shutdown()
    httpd.server_close()
    ga._JOBS.clear()


def call(app, method, path, body=None, cookie=True, hdr=True, raw_resp=False):
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=30)
    h = dict(H) if hdr else {}
    if cookie:
        h["Cookie"] = f"scriptforge_session={app._tok}"
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    if raw_resp:
        return r, data
    return r.status, (json.loads(data) if data[:1] in (b"{", b"[") else data)


def test_logged_out_redirects(web):
    r, _ = call(web, "GET", "/scanruler/", cookie=False, raw_resp=True)
    assert r.status == 302 and r.getheader("Location") == "/"


def test_redirect_slash(web):
    r, _ = call(web, "GET", "/scanruler", raw_resp=True)
    assert r.status == 302 and r.getheader("Location") == "/scanruler/"


def test_index_headers(web):
    r, body = call(web, "GET", "/scanruler/", raw_resp=True)
    assert r.status == 200 and b"app-1.js" in body
    assert "frame-ancestors 'self'" in r.getheader("Content-Security-Policy")
    assert "wasm-unsafe-eval" in r.getheader("Content-Security-Policy")
    assert r.getheader("X-Frame-Options") == "SAMEORIGIN"
    assert r.getheader("Referrer-Policy") == "same-origin"
    assert r.getheader("Cache-Control") == "no-cache"


def test_asset_cache_and_mime(web):
    r, body = call(web, "GET", "/scanruler/assets/a.wasm", raw_resp=True)
    assert r.status == 200 and body.startswith(b"\0asm")
    assert r.getheader("Content-Type") == "application/wasm"
    assert r.getheader("Cache-Control") == "public, max-age=31536000, immutable"
    r, _ = call(web, "GET", "/scanruler/assets/app-1.js", raw_resp=True)
    assert r.getheader("Content-Type").startswith("text/javascript")


def test_worker_csp(web):
    r, _ = call(web, "GET", "/scanruler/assets/meshWorker-abc.js", raw_resp=True)
    assert "'unsafe-eval'" in r.getheader("Content-Security-Policy")
    r, _ = call(web, "GET", "/scanruler/assets/app-1.js", raw_resp=True)
    assert "'unsafe-eval'" not in r.getheader("Content-Security-Policy")


def test_spa_fallback_and_asset_404(web):
    r, body = call(web, "GET", "/scanruler/s/some-name", raw_resp=True)
    assert r.status == 200 and b"app-1.js" in body
    assert call(web, "GET", "/scanruler/assets/missing.js")[0] == 404


def test_root_logo(web):
    (web.home / "v0.11.1" / "dist-sf" / "scanruler-mark.svg").write_text("<svg/>")
    r, body = call(web, "GET", "/scanruler-mark.svg", raw_resp=True)
    assert r.status == 200 and body == b"<svg/>"
    assert r.getheader("Content-Type").startswith("image/svg+xml")
    r, body = call(web, "HEAD", "/scanruler-mark.svg", raw_resp=True)
    assert r.status == 200 and body == b""
    r, _ = call(web, "GET", "/scanruler-mark.svg", cookie=False, raw_resp=True)
    assert r.status == 302 and r.getheader("Location") == "/"


def test_head(web):
    r, body = call(web, "HEAD", "/scanruler/", raw_resp=True)
    assert r.status == 200 and body == b""


@pytest.mark.parametrize("p", ["/scanruler/../../etc/passwd", "/scanruler/%2e%2e/%2e%2e/etc/passwd",
                               "/scanruler/..%2f..%2fetc/passwd", "/scanruler/%00"])
def test_traversal(web, p):
    r, body = call(web, "GET", p, raw_resp=True)
    assert b"root:" not in body
    assert r.status != 200 or b"app-1.js" in body  # refused, or only ever the SPA shell


def test_not_installed(web):
    (web.home / "current").unlink()
    r, body = call(web, "GET", "/scanruler/", raw_resp=True)
    assert r.status == 503 and b"scriptforge scanruler update" in body


def test_symlink_swap_is_live(web):
    new = web.home / "v0.12.0" / "dist-sf"
    new.mkdir(parents=True)
    (new / "index.html").write_text("NEW")
    (web.home / "current").unlink()
    (web.home / "current").symlink_to("v0.12.0/dist-sf")
    assert call(web, "GET", "/scanruler/")[1] == b"NEW"


def test_existing_responses_keep_csp(web):
    r, _ = call(web, "GET", "/", raw_resp=True)
    assert r.getheader("X-Frame-Options") == "DENY" and "frame-ancestors 'none'" in r.getheader("Content-Security-Policy")


# --- scans


def put(web, name, data=PLY, **kw):
    return call(web, "PUT", f"/api/scans?name={name}", body=data, **kw)


def test_upload_list_file_delete(web):
    s, d = put(web, "my%20scan.PLY")
    assert s == 200 and d["name"] == "my_scan.PLY" and d["size"] == len(PLY) and len(d["id"]) == 12
    assert call(web, "GET", "/api/scans")[1]["scans"][0]["id"] == d["id"]
    s, b = call(web, "GET", f"/api/scans/{d['id']}/file/my_scan.PLY")
    assert s == 200 and b == PLY
    assert call(web, "GET", f"/api/scans/{d['id']}/file/other.ply")[0] == 404
    assert call(web, "DELETE", f"/api/scans/{d['id']}")[1] == {"ok": True}
    assert call(web, "DELETE", f"/api/scans/{d['id']}")[0] == 404
    assert call(web, "GET", "/api/scans")[1] == {"scans": []}
    assert not list((web.root / "scans").glob("*/*"))


def test_upload_rejections(web, monkeypatch):
    assert put(web, "a.txt")[0] == 415
    assert put(web, "a.ply", hdr=False)[0] == 403
    assert put(web, "a.ply", cookie=False)[0] == 401
    monkeypatch.setattr(sa, "MAX_SCAN", 10)
    assert put(web, "a.ply")[0] == 413
    assert call(web, "GET", "/api/scans")[1] == {"scans": []}


def test_prune_to_20(web):
    ids = [put(web, f"s{i}.stl")[1]["id"] for i in range(22)]
    got = [s["id"] for s in call(web, "GET", "/api/scans")[1]["scans"]]
    assert len(got) == 20 and ids[-1] in got and ids[0] not in got


# --- check.scanruler


def fake_job(web, with_stl=True):
    out = web.root / "out/web/j"
    out.mkdir(parents=True)
    if with_stl:
        (out / "part_local.stl").write_bytes(STL)
    job = ga.Job("r", "My Bracket")
    job.finish("done", {"ok": True, "out_dir": "out/web/j", "script": None})
    ga._JOBS.append(job)
    return job


def test_check_scanruler(web):
    sid = put(web, "bracket.ply")[1]["id"]
    job = fake_job(web)
    s, z = call(web, "GET", f"/api/scans/{sid}/check/{job.id}/-/check.scanruler")
    assert s == 200
    zf = zipfile.ZipFile(io.BytesIO(z))
    assert zf.namelist() == ["project.json", "scan.ply", "reference.stl"]
    assert zf.read("scan.ply") == PLY and zf.read("reference.stl") == STL
    m = json.loads(zf.read("project.json"))
    assert m["workspace"] == "deviation" and m["name"] == "My_Bracket"
    assert m["appVersion"] == "0.11.1"
    assert m["scan"]["fileName"] == "bracket.ply" and m["scan"]["member"] == "scan.ply"
    assert m["deviation"]["reference"] == {"fileName": "My_Bracket.stl", "member": "reference.stl"}
    assert m["deviation"]["source"] == "reference"


def test_check_missing_stl_and_model_route(web):
    sid = put(web, "b.stl")[1]["id"]
    job = fake_job(web, with_stl=False)
    assert call(web, "GET", f"/api/scans/{sid}/check/{job.id}/-/check.scanruler")[0] == 404
    assert call(web, "GET", f"/api/generate/{job.id}/model/-/model.stl")[0] == 404
    (web.root / "out/web/j/part_local.stl").write_bytes(STL)
    assert call(web, "GET", f"/api/generate/{job.id}/model/-/model.stl")[1] == STL


def test_scanruler_status_and_update(web, monkeypatch):
    s, d = call(web, "GET", "/api/scanruler")
    assert d["installed"] and d["version"] == "0.11.1" and d["updating"] is False
    ran = threading.Event()
    monkeypatch.setattr(sa.sr, "update", lambda: ran.set())
    assert call(web, "POST", "/api/scanruler/update")[1] == {"started": True}
    assert ran.wait(5)
