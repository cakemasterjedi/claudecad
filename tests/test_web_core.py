"""fsgen web: server core (auth, CSRF, headers, paths, SSE, usage). Localhost only, no network."""
import http.client
import json
import threading

import pytest

from fsgen.web import server as srv
from fsgen.web.paths import list_scripts, resolve_in_root
from fsgen.web.server import HTTPError, LoginLimiter, SessionStore, route

H = {"X-Fsgen": "1"}


@route("GET", "/api/_test/echo/{name}")
def _echo(req):
    return {"name": req.params["name"], "q": req.query, "root": str(req.app.root)}


@route("POST", "/api/_test/post")
def _post(req):
    return {"got": req.body}


@route("GET", "/api/_test/stream")
def _stream(req):
    def gen():
        for i in range(3):
            yield "tick", {"i": i}
    return gen()


@route("GET", "/api/_test/boom")
def _boom(req):
    raise HTTPError(418, "teapot")


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    root = tmp_path_factory.mktemp("root")
    httpd, app = srv.make_server(root, "127.0.0.1", 0, "pw")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1], app
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def _reset(web):
    web[1].limiter = LoginLimiter()
    web[1].sse_keepalive = 15.0


def call(web, method, path, body=None, headers=None, cookie=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", web[0], timeout=10)
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = f"fsgen_session={cookie}"
    data = raw if raw is not None else (json.dumps(body) if body is not None else None)
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    payload = r.read()
    return r, payload


def login(web, pw="pw"):
    r, p = call(web, "POST", "/api/login", {"password": pw}, H)
    return r, p


def session(web):
    r, _ = login(web)
    assert r.status == 200
    return r.getheader("Set-Cookie").split(";")[0].split("=", 1)[1]


def test_login_success_and_cookie_flags(web):
    r, p = login(web)
    assert r.status == 200 and json.loads(p) == {"ok": True}
    cookie = r.getheader("Set-Cookie")
    assert cookie.startswith("fsgen_session=")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert len(cookie.split(";")[0].split("=", 1)[1]) >= 43  # 32 random bytes, base64url


def test_login_failure(web):
    r, p = login(web, "nope")
    assert r.status == 401 and r.getheader("Set-Cookie") is None
    r, _ = call(web, "POST", "/api/login", raw="not json", headers=H)
    assert r.status == 400


def test_api_requires_session(web):
    r, p = call(web, "GET", "/api/usage")
    assert r.status == 401 and "error" in json.loads(p)
    r, _ = call(web, "GET", "/api/_test/echo/x", cookie="bogus")
    assert r.status == 401


def test_logout_invalidates(web):
    tok = session(web)
    assert call(web, "GET", "/api/_test/echo/x", cookie=tok)[0].status == 200
    assert call(web, "POST", "/api/logout", headers=H, cookie=tok)[0].status == 200
    assert call(web, "GET", "/api/_test/echo/x", cookie=tok)[0].status == 401


def test_csrf_header_required(web):
    r, _ = call(web, "POST", "/api/login", {"password": "pw"})
    assert r.status == 403
    tok = session(web)
    r, _ = call(web, "POST", "/api/_test/post", {"a": 1}, cookie=tok)
    assert r.status == 403
    r, p = call(web, "POST", "/api/_test/post", {"a": 1}, H, cookie=tok)
    assert r.status == 200 and json.loads(p) == {"got": {"a": 1}}


def test_rate_limit(web):
    for _ in range(5):
        assert login(web, "bad")[0].status == 401
    assert login(web, "bad")[0].status == 429
    assert login(web, "pw")[0].status == 429  # locked out even with the right password


def test_limiter_unit():
    now = [0.0]
    lim = LoginLimiter(clock=lambda: now[0])
    for _ in range(4):
        lim.failure("a")
    assert not lim.blocked("a")
    lim.failure("a")
    assert lim.blocked("a") and not lim.blocked("b")
    now[0] = 599
    assert lim.blocked("a")
    now[0] = 601
    assert not lim.blocked("a")
    # failures outside the window do not add up
    for t in (0, 200, 400, 650, 700):
        now[0] = 700 + t
        lim.failure("c")
    assert not lim.blocked("c")


def test_session_expiry_unit():
    now = [0.0]
    s = SessionStore(idle=100, clock=lambda: now[0])
    t = s.create()
    now[0] = 90
    assert s.valid(t)       # refreshes the idle timer
    now[0] = 180
    assert s.valid(t)
    now[0] = 281
    assert not s.valid(t)
    assert not s.valid(None) and not s.valid("x")
    assert SessionStore().idle == 12 * 3600


def test_route_params_query_errors(web):
    tok = session(web)
    r, p = call(web, "GET", "/api/_test/echo/a%20b?x=1", cookie=tok)
    d = json.loads(p)
    assert d["name"] == "a b" and d["q"] == {"x": "1"}
    assert call(web, "GET", "/api/_test/boom", cookie=tok)[0].status == 418
    assert call(web, "GET", "/api/nothing", cookie=tok)[0].status == 404
    assert call(web, "POST", "/api/_test/boom", headers=H, cookie=tok)[0].status == 405


def test_sse_stream(web):
    tok = session(web)
    r, p = call(web, "GET", "/api/_test/stream", cookie=tok)
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
    assert p.decode() == "".join(f'event: tick\ndata: {{"i": {i}}}\n\n' for i in range(3))


def test_sse_keepalive(web):
    import time

    @route("GET", "/api/_test/slow")
    def slow(req):
        def gen():
            time.sleep(0.35)
            yield "late", {}
        return gen()

    web[1].sse_keepalive = 0.1
    tok = session(web)
    _, p = call(web, "GET", "/api/_test/slow", cookie=tok)
    assert b": keep-alive\n\n" in p and b"event: late" in p


def test_body_cap(web):
    tok = session(web)
    big = '{"a":"' + "x" * (srv.MAX_BODY + 100_000) + '"}'
    r, _ = call(web, "POST", "/api/_test/post", headers=H, cookie=tok, raw=big)
    assert r.status == 413
    r, _ = call(web, "POST", "/api/_test/post", headers=H, cookie=tok, raw="{bad")
    assert r.status == 400


def test_security_headers(web):
    tok = session(web)
    for path, ck in (("/", None), ("/static/app.js", None), ("/api/usage", None), ("/api/_test/stream", tok),
                     ("/nonexistent", None)):
        r, _ = call(web, "GET", path, cookie=ck)
        assert r.getheader("X-Content-Type-Options") == "nosniff", path
        assert r.getheader("Referrer-Policy") == "no-referrer"
        assert r.getheader("X-Frame-Options") == "DENY"
        csp = r.getheader("Content-Security-Policy")
        assert "script-src 'self' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net" in csp
        assert "connect-src 'self'" in csp and "img-src 'self' data: blob:" in csp
        assert "unsafe-inline" not in csp


def test_static(web):
    r, p = call(web, "GET", "/")
    assert r.status == 200 and b"fsgen" in p and r.getheader("Content-Type").startswith("text/html")
    assert call(web, "GET", "/static/app.js")[0].status == 200
    r, p = call(web, "GET", "/static/parts.js")  # optional tab module: empty when missing
    assert r.status == 200
    assert call(web, "GET", "/static/missing.js")[0].status == 404


@pytest.mark.parametrize("path", ["/static/../server.py", "/static/%2e%2e/server.py", "/static/..%2fserver.py",
                                  "/static/a%00.js", "/static//etc/passwd"])
def test_static_traversal(web, path):
    r, p = call(web, "GET", path)
    assert r.status in (400, 403, 404) and b"ThreadingHTTPServer" not in p


def test_resolve_in_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.fs").write_text("x")
    outside = tmp_path / "secret.fs"
    outside.write_text("s")
    (root / "link.fs").symlink_to(outside)
    (root / "sub").mkdir()
    assert resolve_in_root(root, "a.fs") == (root / "a.fs").resolve()
    assert resolve_in_root(root, "sub/new.fs").name == "new.fs"  # not existing yet is fine
    for rel, status in (("/etc/passwd", 400), (str(outside), 400), ("../secret.fs", 400), ("sub/../../secret.fs", 400),
                        ("a\0.fs", 400), ("a.txt", 400), ("link.fs", 403), ("", 400)):
        with pytest.raises(HTTPError) as e:
            resolve_in_root(root, rel)
        assert e.value.status == status, rel
    assert resolve_in_root(root, "a.txt", suffixes=None).name == "a.txt"


def test_list_scripts(tmp_path):
    root = tmp_path / "root"
    for d in ("sub", "out", ".hidden", ".venv-x", "node_modules", "__pycache__", "sub/out"):
        (root / d).mkdir(parents=True)
        (root / d / "x.fs").write_text("1")
    (root / "b.fs").write_text("1")
    (root / "a.fs").write_text("1")
    (root / "notes.txt").write_text("1")
    outside = tmp_path / "o.fs"
    outside.write_text("1")
    (root / "esc.fs").symlink_to(outside)
    (root / "sub" / "x.fs").write_text("1")
    assert list_scripts(root) == ["a.fs", "b.fs", "sub/x.fs"]


def test_list_scripts_cap(tmp_path):
    for i in range(2005):
        (tmp_path / f"f{i:04d}.fs").write_text("")
    assert len(list_scripts(tmp_path)) == 2000


def test_usage_endpoint(web, tmp_path, monkeypatch):
    for k, v in (("ONSHAPE_API_ALLOCATION", "100"), ("ONSHAPE_API_BASELINE", "10"), ("ONSHAPE_API_BASELINE_DATE", "")):
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("fsgen.onshape.LEDGER", __import__("pathlib").Path(".fsgen_api_ledger.jsonl"))
    monkeypatch.setattr("fsgen.usage.LEDGER", __import__("pathlib").Path(".fsgen_api_ledger.jsonl"))
    rows = [
        {"t": "2026-01-01T00:00:00", "run": "r", "purpose": "generate:x", "call": "GET /documents", "status": 200, "counted": True},
        {"t": "2026-01-01T00:00:01", "run": "r", "purpose": "generate:y", "call": "GET /documents", "status": 200, "counted": True},
        {"t": "2026-01-01T00:00:02", "run": "r", "purpose": "studio push", "call": "POST /features", "status": 200, "counted": True},
        {"t": "2026-01-01T00:00:03", "run": "r", "purpose": "studio push", "call": "POST /features", "status": 500, "counted": False},
    ]
    (tmp_path / ".fsgen_api_ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    tok = session(web)
    r, p = call(web, "GET", "/api/usage", cookie=tok)
    d = json.loads(p)
    assert r.status == 200
    assert (d["allocation"], d["used"], d["left"], d["percent"], d["logged"]) == (100, 13, 87, 13.0, 3)
    assert d["by_purpose"][0] == {"name": "generate", "count": 2}
    assert d["top_endpoints"][0] == {"name": "GET /documents", "count": 2}
    assert not (tmp_path / ".env").exists()


def test_cli_web_parser(monkeypatch):
    from fsgen import cli
    seen = {}
    monkeypatch.setattr(cli, "cmd_web", lambda a: seen.setdefault("a", a) and 0)
    with pytest.raises(SystemExit):
        cli.main(["web"])
    a = seen["a"]
    assert (a.host, a.port, a.password, a.root) == ("0.0.0.0", 8770, None, ".")


def test_route_modules_registered():
    from fsgen.web import ROUTE_MODULES
    assert "fsgen.web.usage_api" in ROUTE_MODULES
