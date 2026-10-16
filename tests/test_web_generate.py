"""fsgen web Generate API with a fake LLM (no Claude, no Onshape)."""
import http.client
import json
import threading
import time
from pathlib import Path

import pytest

import fsgen.web.generate_api as ga  # registers the routes
from fsgen.native import generate as gen
from fsgen.web import server as srv
from fsgen.web.server import LoginLimiter

H = {"X-Fsgen": "1"}
PLATE = (Path(__file__).resolve().parent.parent / "examples/native/plate.fs").read_text()
REPLY = f"Assumptions:\n- plate is 80 x 50 mm\n\n```featurescript\n{PLATE}\n```"


class FakeLLM:
    name = "fake"

    def __init__(self, gate=None):
        self.gate, self.prompts = gate, []

    def complete(self, system, prompt, image=None, images=()):
        self.prompts.append(prompt)
        if self.gate is not None:
            assert self.gate.wait(20)
        return "Fine.\nLGTM" if "review" in prompt.lower()[:200] and len(self.prompts) > 1 else REPLY


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONSHAPE_ACCESS_KEY", "a")
    monkeypatch.setenv("ONSHAPE_SECRET_KEY", "b")
    monkeypatch.setattr(gen, "render", lambda *a, **k: None)
    ga._JOBS.clear()
    root = tmp_path / "root"
    root.mkdir()
    httpd, app = srv.make_server(root, "127.0.0.1", 0, "pw")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    app.port = httpd.server_address[1]
    yield app
    httpd.shutdown()
    httpd.server_close()
    ga._JOBS.clear()


def use_llm(monkeypatch, fake):
    monkeypatch.setattr(gen, "make_llm", lambda *a, **k: fake)
    return fake


def call(app, method, path, body=None, cookie=True, headers=H):
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=30)
    h = dict(headers)
    if cookie:
        h["Cookie"] = f"fsgen_session={app._tok}"
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    return r.status, (json.loads(data) if data and data[:1] in b"{[" else data)


@pytest.fixture(autouse=True)
def _login(web):
    web.limiter = LoginLimiter()
    web._tok = web.sessions.create()


def events(app, jid, frm=0):
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=60)
    c.request("GET", f"/api/generate/{jid}/events?from={frm}", headers={"Cookie": f"fsgen_session={app._tok}"})
    r = c.getresponse()
    assert r.status == 200
    out, ev = [], None
    for raw in r.read().decode().split("\n\n"):
        d = {}
        for line in raw.split("\n"):
            if line.startswith("event:"):
                d["event"] = line[6:].strip()
            elif line.startswith("data:"):
                d["data"] = json.loads(line[5:])
        if "event" in d:
            out.append((d["event"], d["data"]))
    return out


def wait_done(app, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s, d = call(app, "GET", "/api/generate/current")
        if d["job"] and d["job"]["status"] != "running":
            return d["job"]
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_job_streams_logs_saves_script_without_overwrite(web, monkeypatch):
    use_llm(monkeypatch, FakeLLM())
    seen = {}
    real = gen.generate_native

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)
    monkeypatch.setattr(gen, "generate_native", spy)
    (web.root / "Mount.fs").write_text("existing")
    s, d = call(web, "POST", "/api/generate", {"request": "a mounting plate", "name": "Mount", "review_rounds": 1})
    assert s == 200
    evs = events(web, d["id"])
    assert evs[-1][0] == "done" and evs[-1][1]["status"] == "done"
    logs = [e for e in evs if e[0] == "log"]
    assert logs and [e[1]["i"] for e in logs] == list(range(len(logs)))
    assert any("paste mode: no Onshape calls" in e[1]["text"] for e in logs)
    res = evs[-1][1]["result"]
    assert res["ok"] and res["script"] == "Mount-2.fs" and res["api_calls"] == 0
    assert (web.root / "Mount.fs").read_text() == "existing"
    assert "function build" in (web.root / "Mount-2.fs").read_text()
    assert "80 x 50" in res["assumptions"]
    assert res["out_dir"].startswith("out/web/") and (web.root / res["out_dir"] / "part_studio.fs").exists()
    assert seen["paste"] is True and seen["budget"] == 0 and seen["export_formats"] == ()
    assert seen["web"] is True and seen["max_fixes"] == 4 and seen["review_rounds"] == 1
    s, cur = call(web, "GET", "/api/generate/current")
    assert cur["job"]["id"] == d["id"] and cur["job"]["status"] == "done" and cur["job"]["log_lines"] == len(logs)


def test_reattach_from_n_and_busy_409(web, monkeypatch):
    gate = threading.Event()
    use_llm(monkeypatch, FakeLLM(gate))
    s, d = call(web, "POST", "/api/generate", {"request": "a plate", "review_rounds": 0})
    assert s == 200
    s, e = call(web, "POST", "/api/generate", {"request": "another"})
    assert s == 409
    for _ in range(100):
        if ga._JOBS[-1].lines:
            break
        time.sleep(0.05)
    gate.set()
    job = wait_done(web)
    assert job["status"] == "done"
    full = [e for e in events(web, d["id"]) if e[0] == "log"]
    assert len(full) > 2
    part = events(web, d["id"], 2)
    assert [e[1]["i"] for e in part if e[0] == "log"] == list(range(2, len(full)))
    assert part[-1][0] == "done"
    # a new job is allowed once the first is over
    s, _ = call(web, "POST", "/api/generate", {"request": "again", "review_rounds": 0})
    assert s == 200
    wait_done(web)


def test_missing_keys_in_paste_mode_still_work(web, monkeypatch):
    monkeypatch.delenv("ONSHAPE_ACCESS_KEY")
    monkeypatch.delenv("ONSHAPE_SECRET_KEY")
    monkeypatch.setattr("fsgen.onshape.load_dotenv", lambda *a, **k: None)
    use_llm(monkeypatch, FakeLLM())
    call(web, "POST", "/api/generate", {"request": "a plate", "review_rounds": 0})
    job = wait_done(web)
    assert job["status"] == "done" and job["result"]["api_calls"] == 0


def test_job_error_is_reported(web, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm exploded")
    monkeypatch.setattr(gen, "make_llm", boom)
    call(web, "POST", "/api/generate", {"request": "a plate"})
    job = wait_done(web)
    assert job["status"] == "failed" and "llm exploded" in job["result"]["error"]


def test_validation_and_auth(web, monkeypatch):
    use_llm(monkeypatch, FakeLLM(threading.Event()))  # never started: all requests below are rejected
    assert call(web, "POST", "/api/generate", {"request": ""})[0] == 400
    assert call(web, "POST", "/api/generate", {"request": "x" * 4001})[0] == 400
    assert call(web, "POST", "/api/generate", {"nope": 1})[0] == 400
    assert call(web, "POST", "/api/generate", {"request": "x"}, cookie=False)[0] == 401
    assert call(web, "POST", "/api/generate", {"request": "x"}, headers={})[0] == 403
    assert call(web, "GET", "/api/generate/current", cookie=False)[0] == 401
    assert call(web, "GET", "/api/generate/nope/events")[0] == 404
    assert call(web, "GET", "/api/generate/current")[1] == {"job": None}


def test_name_sanitizing_and_clamping():
    assert ga.sanitize_name("../../etc/pa ss.fs", "x") == "etc-pa-ss-fs"
    assert ga.sanitize_name(None, "A small bracket, 40 mm wide!") == "A-small-bracket-40-mm"
    assert ga.sanitize_name("   ", "") == "part"
    assert ga.sanitize_name("é" * 5, "x") == "part"
    assert len(ga.sanitize_name("a" * 200, "x")) == 60
    assert ga._clamp(99, 0, 8, 4) == 8 and ga._clamp(-3, 0, 8, 4) == 0 and ga._clamp("x", 0, 8, 4) == 4


def test_name_sanitized_and_options_clamped_in_request(web, monkeypatch):
    use_llm(monkeypatch, FakeLLM())
    seen = {}
    real = gen.generate_native
    monkeypatch.setattr(gen, "generate_native", lambda *a, **k: (seen.update(k), real(*a, **k))[1])
    call(web, "POST", "/api/generate", {"request": "a plate", "name": "../evil name", "max_fixes": 50,
                                        "review_rounds": 9, "web_search": False})
    job = wait_done(web)
    assert job["name"] == "evil-name" and job["result"]["script"] == "evil-name.fs"
    assert seen["max_fixes"] == 8 and seen["review_rounds"] == 3 and seen["web"] is False
    assert seen["name"] == "evil-name"
