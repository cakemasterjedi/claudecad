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


def b64(b):
    return base64.b64encode(b).decode()


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


def up(app, data=JPEG, **kw):
    return call(app, "POST", "/api/uploads", {"filename": "a.jpg", "data": b64(data), **kw})


def test_upload_jpeg_png_saved(web):
    s, d = up(web)
    assert s == 200 and d["type"] == "jpeg" and len(d["id"]) == 12
    assert (web.root / "out/web/uploads" / f"{d['id']}.jpeg").read_bytes() == JPEG
    s, d = call(web, "POST", "/api/uploads", {"filename": "a.png", "data": "data:image/png;base64," + b64(PNG)})
    assert s == 200 and d["type"] == "png"
    assert (web.root / "out/web/uploads" / f"{d['id']}.png").read_bytes() == PNG


def test_upload_rejects(web):
    assert up(web, b"hello world")[0] == 415
    assert call(web, "POST", "/api/uploads", {"filename": "a", "data": "!!not base64!!"})[0] == 400
    assert call(web, "POST", "/api/uploads", {"filename": "a"})[0] == 400
    assert up(web, JPEG + b"\0" * 3_000_001)[0] == 413
    assert call(web, "POST", "/api/uploads", {"data": b64(JPEG)}, cookie=False)[0] == 401


def test_upload_prunes_old(web, monkeypatch):
    monkeypatch.setattr(ua, "MAX_UPLOADS", 3)
    for _ in range(5):
        assert up(web)[0] == 200
    assert len(list((web.root / "out/web/uploads").iterdir())) == 3


def test_measure(web, monkeypatch):
    seen = {}

    class Fake:
        name = "fake"

        def complete(self, system, prompt, image=None, images=()):
            seen.update(prompt=prompt, images=list(images))
            return " - 80 mm wide \n"
    monkeypatch.setattr(ua, "claude_llm", lambda *a, **k: Fake())
    monkeypatch.setattr(ua, "OllamaLLM", lambda *a, **k: Fake())
    iid = up(web)[1]["id"]
    s, d = call(web, "POST", "/api/measure", {"images": [iid], "note": "inches", "backend": "claude"})
    assert s == 200 and d["text"] == "- 80 mm wide" and d["backend"] == "claude" and "seconds" in d
    assert "inches" in seen["prompt"] and seen["images"][0].name == f"{iid}.jpeg"
    s, d = call(web, "POST", "/api/measure", {"images": [iid], "backend": "ollama"})
    assert s == 200 and d["backend"] == "ollama"
    assert call(web, "POST", "/api/measure", {"images": ["0" * 12]})[0] == 400
    assert call(web, "POST", "/api/measure", {"images": ["../../etc/passwd"]})[0] == 400
    assert call(web, "POST", "/api/measure", {"images": []})[0] == 400
    assert call(web, "POST", "/api/measure", {"images": [iid], "backend": "x"})[0] == 400


def test_measure_llm_error_502_and_busy_409(web, monkeypatch):
    class Bad:
        def complete(self, *a, **k):
            raise LLMError("ollama down")
    monkeypatch.setattr(ua, "OllamaLLM", lambda *a, **k: Bad())
    iid = up(web)[1]["id"]
    s, d = call(web, "POST", "/api/measure", {"images": [iid], "backend": "ollama"})
    assert s == 502 and "ollama down" in d["error"]
    assert ua._MEASURE_LOCK.acquire(blocking=False)
    try:
        assert call(web, "POST", "/api/measure", {"images": [iid], "backend": "ollama"})[0] == 409
    finally:
        ua._MEASURE_LOCK.release()


def test_llm_info(web, monkeypatch):
    monkeypatch.setattr(ua, "ollama_status", lambda: {"available": True, "model": "m", "models": ["m"]})
    s, d = call(web, "GET", "/api/llm")
    assert s == 200 and d["measure_default"] == "ollama" and d["ollama"]["model"] == "m"
    monkeypatch.setattr(ua, "ollama_status", lambda: {"available": False, "model": "m", "models": []})
    assert call(web, "GET", "/api/llm")[1]["measure_default"] == "claude"


def test_generate_passes_images(web, monkeypatch):
    seen = {}

    def fake(request, out_dir, **k):
        seen.update(k)
        return {"ok": False, "error": "stop", "api_calls": 0}
    monkeypatch.setattr(gen, "generate_native", fake)
    iid = up(web)[1]["id"]
    s, d = call(web, "POST", "/api/generate", {"request": "a plate", "images": [iid]})
    assert s == 200
    for _ in range(100):
        if ga._JOBS[-1].status != "running":
            break
        time.sleep(0.05)
    assert [p.name for p in seen["images"]] == [f"{iid}.jpeg"]
    assert call(web, "GET", "/api/generate/current")[1]["job"]["images"] == 1
    assert call(web, "POST", "/api/generate", {"request": "a plate", "images": ["f" * 12]})[0] == 400
    assert call(web, "POST", "/api/generate", {"request": "a plate", "images": [iid] * 5})[0] == 400
