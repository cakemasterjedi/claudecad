"""fsgen web uploads / measure / generate-with-images (no network, no real LLM)."""
import base64
import http.client
import json
import threading
import time

import pytest

import fsgen.web.generate_api as ga
import fsgen.web.uploads_api as ua
from fsgen.llm import LLMError
from fsgen.native import generate as gen
from fsgen.web import server as srv
from fsgen.web.server import LoginLimiter

H = {"X-Fsgen": "1"}
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


@pytest.fixture
def web(tmp_path):
    ga._JOBS.clear()
    root = tmp_path / "root"
    root.mkdir()
    httpd, app = srv.make_server(root, "127.0.0.1", 0, "pw")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    app.port = httpd.server_address[1]
    app.limiter = LoginLimiter()
    app._tok = app.sessions.create()
    yield app
    httpd.shutdown()
    httpd.server_close()
    ga._JOBS.clear()


def call(app, method, path, body=None, cookie=True):
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=30)
    h = dict(H)
    if cookie:
        h["Cookie"] = f"fsgen_session={app._tok}"
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    return r.status, (json.loads(data) if data[:1] in (b"{", b"[") else data)


def test_change_password(web):
    other = web.sessions.create()
    s, d = call(web, "POST", "/api/password", {"current": "pw", "new": "brandnew123"})
    assert s == 200 and web.password == "brandnew123"
    assert "FSGEN_WEB_PASSWORD='brandnew123'" in (web.root / ".env").read_text()
    assert web.sessions.valid(web._tok) and not web.sessions.valid(other)


def test_change_password_rejects_wrong_current_and_weak_new(web):
    assert call(web, "POST", "/api/password", {"current": "nope", "new": "brandnew123"})[0] == 403
    assert call(web, "POST", "/api/password", {"current": "pw", "new": "short"})[0] == 400
    assert call(web, "POST", "/api/password", {"current": "pw"})[0] == 400
    assert web.password == "pw" and not (web.root / ".env").exists()
    assert call(web, "POST", "/api/password", {"current": "pw", "new": "brandnew123"}, cookie=False)[0] == 401
