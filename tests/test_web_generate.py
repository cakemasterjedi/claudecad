"""scriptforge web Generate API with a fake LLM (no Claude, no Onshape)."""
import http.client
import json
import threading
import time
from pathlib import Path

import pytest

import scriptforge.web.generate_api as ga  # registers the routes
from scriptforge.native import generate as gen
from scriptforge.web import server as srv
from scriptforge.web.server import LoginLimiter

H = {"X-ScriptForge": "1"}
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
        h["Cookie"] = f"scriptforge_session={app._tok}"
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
    c.request("GET", f"/api/generate/{jid}/events?from={frm}", headers={"Cookie": f"scriptforge_session={app._tok}"})
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
    monkeypatch.setattr("scriptforge.onshape.load_dotenv", lambda *a, **k: None)
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


def test_generate_forced_local_model(web, monkeypatch):
    import scriptforge.web.uploads_api as ua
    monkeypatch.setattr(ua, "ollama_status", lambda: {"available": True, "model": "m", "models": ["m", "q:7b"]})
    use_llm(monkeypatch, FakeLLM())
    seen = []
    real = gen.generate_native
    monkeypatch.setattr(gen, "generate_native", lambda *a, **k: (seen.append(k), real(*a, **k))[1])
    s, d = call(web, "POST", "/api/generate", {"request": "a plate", "local_model": "q:7b"})
    assert s == 200
    job = wait_done(web)
    assert job["model"] == "q:7b" and seen[0]["backend"] == "ollama" and seen[0]["model"] == "q:7b"
    s, d = call(web, "POST", "/api/generate", {"request": "a plate", "local_model": "nope"})
    assert s == 400 and "not installed" in d["error"]
    for bad in (5, ["q:7b"]):
        assert call(web, "POST", "/api/generate", {"request": "a plate", "local_model": bad})[0] == 400
    seen.clear()
    assert call(web, "POST", "/api/generate", {"request": "a plate", "local_model": ""})[0] == 200
    job = wait_done(web)
    assert job["model"] is None and "backend" not in seen[0] and "model" not in seen[0]


# --- download / estimate / push

def finished_job(web, monkeypatch):
    monkeypatch.delenv("SCRIPTFORGE_DOCUMENT", raising=False)
    use_llm(monkeypatch, FakeLLM())
    s, d = call(web, "POST", "/api/generate", {"request": "a mounting plate", "name": "Mount", "review_rounds": 0})
    assert s == 200
    job = wait_done(web)
    assert job["status"] == "done"
    return d["id"], job["result"]


def test_download_script_and_zip(web, monkeypatch):
    import io
    import zipfile
    jid, res = finished_job(web, monkeypatch)
    (web.root / res["out_dir"] / "llm_01_x.md").write_text("big log")
    c = http.client.HTTPConnection("127.0.0.1", web.port, timeout=30)
    c.request("GET", f"/api/generate/{jid}/download?what=script", headers={"Cookie": f"scriptforge_session={web._tok}"})
    r = c.getresponse()
    body = r.read()
    assert r.status == 200 and r.getheader("Content-Disposition") == 'attachment; filename="Mount.fs"'
    assert body == (web.root / res["script"]).read_bytes()
    s, z = call(web, "GET", f"/api/generate/{jid}/download?what=zip")
    assert s == 200
    names = zipfile.ZipFile(io.BytesIO(z)).namelist()
    assert "part_studio.fs" in names and "Mount.fs" in names and not any(n.startswith("llm_") for n in names)
    assert call(web, "GET", f"/api/generate/{jid}/download?what=bogus")[0] == 400
    assert call(web, "GET", "/api/generate/nope/download")[0] == 404
    assert call(web, "GET", f"/api/generate/{jid}/download", cookie=False)[0] == 401


def test_download_stl_makes_no_onshape_calls(web, monkeypatch):
    import scriptforge.onshape as osh
    jid, res = finished_job(web, monkeypatch)
    out = web.root / res["out_dir"]
    (out / "part_local.stl").write_bytes(b"solid x\nendsolid x\n")
    monkeypatch.setattr(osh.Onshape, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("onshape")))
    c = http.client.HTTPConnection("127.0.0.1", web.port, timeout=30)
    c.request("GET", f"/api/generate/{jid}/download?what=stl", headers={"Cookie": f"scriptforge_session={web._tok}"})
    r = c.getresponse()
    body = r.read()
    assert r.status == 200 and r.getheader("Content-Disposition") == 'attachment; filename="Mount.stl"'
    assert body == b"solid x\nendsolid x\n"
    (out / "part_local.stl").unlink()
    s, d = call(web, "GET", f"/api/generate/{jid}/download?what=stl")
    assert s == 404


def test_download_failed_running_and_no_script(web):
    import scriptforge.web.generate_api as g
    out = web.root / "out/web/x-bad"
    out.mkdir(parents=True)
    (out / "part_studio.fs").write_text("last attempt")
    (out / "llm_01.md").write_text("log")
    bad = g.Job("r", "bad")
    bad.finish("failed", {"ok": False, "out_dir": "out/web/x-bad", "script": None, "error": "x"})
    run = g.Job("r", "run")
    g._JOBS.extend([bad, run])
    assert call(web, "GET", f"/api/generate/{bad.id}/download?what=script")[0] == 404
    assert call(web, "GET", f"/api/generate/{bad.id}/estimate")[0] == 404
    s, z = call(web, "GET", f"/api/generate/{bad.id}/download?what=zip")
    assert s == 200 and z[:2] == b"PK"
    assert call(web, "GET", f"/api/generate/{run.id}/download?what=zip")[0] == 409


def test_estimate_makes_no_onshape_calls(web, monkeypatch):
    import scriptforge.onshape as osh
    jid, _ = finished_job(web, monkeypatch)
    monkeypatch.setattr(osh.Onshape, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Onshape used")))
    s, d = call(web, "GET", f"/api/generate/{jid}/estimate")
    assert s == 200 and d["calls"] > 0 and d["mode"] == "new" and d["budget"] == 60
    assert "estimated Onshape API calls" in d["text"] and d["left"] > 0
    assert not Path(".scriptforge_api_ledger.jsonl").exists()


def test_push_success_over_budget_keys_and_lock(web, monkeypatch):
    import scriptforge.native.pushflow as pf
    from scriptforge.native.build import PushResult
    jid, _ = finished_job(web, monkeypatch)
    seen = []

    def fake_push(o, tr, name, text, source, **k):
        seen.append(name)
        return PushResult(True, built=3, reused=0, url="https://cad.onshape.com/documents/x"), None
    monkeypatch.setattr(pf, "push", fake_push)
    s, d = call(web, "POST", f"/api/generate/{jid}/push")
    assert s == 200 and d["ok"] and d["url"].endswith("/documents/x") and d["built"] == 3 and seen == ["Mount"]
    assert call(web, "POST", f"/api/generate/{jid}/push", cookie=False)[0] == 401
    assert call(web, "POST", f"/api/generate/{jid}/push", headers={})[0] == 403
    # failure is a message, not a stack trace
    monkeypatch.setattr(pf, "push", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    s, d = call(web, "POST", f"/api/generate/{jid}/push")
    assert s == 200 and not d["ok"] and "boom" in d["error"]
    # concurrent push
    gate, started = threading.Event(), threading.Event()

    def slow(*a, **k):
        started.set()
        assert gate.wait(20)
        return PushResult(True, url="u"), None
    monkeypatch.setattr(pf, "push", slow)
    t = threading.Thread(target=lambda: call(web, "POST", f"/api/generate/{jid}/push"))
    t.start()
    assert started.wait(20)
    assert call(web, "POST", f"/api/generate/{jid}/push")[0] == 409
    gate.set()
    t.join()
    # over budget
    monkeypatch.setenv("ONSHAPE_RUN_BUDGET", "1")
    s, d = call(web, "POST", f"/api/generate/{jid}/push")
    assert s == 409 and "budget" in d["error"]
    # missing keys
    monkeypatch.delenv("ONSHAPE_RUN_BUDGET")
    monkeypatch.delenv("ONSHAPE_ACCESS_KEY")
    monkeypatch.setattr("scriptforge.usage.load_dotenv", lambda *a, **k: None)  # do not re-read the repo .env
    s, d = call(web, "POST", f"/api/generate/{jid}/push")
    assert s == 400 and "keys" in d["error"]


# --- questions and variants

QUESTIONS = "Questions:\n1. Which style? Options: classic | modern\n2. Which size?\n"


class AskLLM(FakeLLM):
    """Replies with questions when the prompt carries the ask note and no answers yet."""

    def complete(self, system, prompt, image=None, images=()):
        if "reply ONLY with a section headed exactly `Questions:`" in prompt:
            self.prompts.append(prompt)
            return QUESTIONS
        return super().complete(system, prompt, image=image, images=images)


def post(web, **body):
    body.setdefault("request", "a plate")
    body.setdefault("review_rounds", 0)
    s, d = call(web, "POST", "/api/generate", body)
    assert s == 200, d
    return wait_done(web)


def test_ask_returns_questions_and_job_is_done(web, monkeypatch):
    fake = use_llm(monkeypatch, AskLLM())
    job = post(web, ask=True)
    r = job["result"]
    assert job["status"] == "done" and r["ok"] is False and r["error"] is None
    assert r["questions"] == [{"text": "Which style?", "options": ["classic", "modern"], "compare": False},
                              {"text": "Which size?", "options": [], "compare": False}]
    assert len(fake.prompts) == 1


def test_no_ask_has_empty_questions(web, monkeypatch):
    use_llm(monkeypatch, AskLLM())
    assert post(web)["result"]["questions"] == []


def test_answers_go_into_the_prompt_and_ask_is_off(web, monkeypatch):
    fake = use_llm(monkeypatch, AskLLM())
    job = post(web, ask=True, answers=[{"question": "Which style?", "answer": "modern"}])
    assert job["status"] == "done" and job["result"]["ok"]
    assert "Answers to your clarifying questions:" in fake.prompts[0] and "A: modern" in fake.prompts[0]
    assert "`Questions:`" not in fake.prompts[0]
    assert job["request"] == "a plate"


def test_answers_already_in_the_text_turn_ask_off(web, monkeypatch):
    fake = use_llm(monkeypatch, AskLLM())
    job = post(web, ask=True, request="a ring\n\nAnswers to your clarifying questions:\n- Q: Top?\n  A: flat")
    assert job["status"] == "done" and job["result"]["ok"] and job["result"]["questions"] == []
    assert "`Questions:`" not in fake.prompts[0]


def test_variants_run_twice(web, monkeypatch):
    fake = use_llm(monkeypatch, FakeLLM())
    job = post(web, name="Ring", variants=["classic top", ""])
    r = job["result"]
    assert job["status"] == "done" and r["ok"] and r["questions"] == []
    a, b = r["variants"]
    assert (a["label"], b["label"]) == ("A", "B") and a["note"] == "classic top"
    assert "different alternative to version A" in b["note"] and "plate is 80 x 50" in b["note"]
    assert "Make this version: classic top" in fake.prompts[0]
    assert "Make this version: a clearly different" in fake.prompts[1]
    assert a["script"] == "Ring-A.fs" and b["script"] == "Ring-B.fs"
    assert a["out_dir"].endswith("/A") and b["out_dir"].endswith("/B")
    s, lines = call(web, "GET", f"/api/generate/{job['id']}/events")
    assert b"[A] " in lines and b"[B] " in lines


def test_variant_download_estimate_push(web, monkeypatch):
    import scriptforge.native.pushflow as pf
    from scriptforge.native.build import PushResult
    monkeypatch.delenv("SCRIPTFORGE_DOCUMENT", raising=False)
    use_llm(monkeypatch, FakeLLM())
    job = post(web, name="Ring", variants=["", ""])
    jid = job["id"]
    (web.root / "Ring-B.fs").write_text((web.root / "Ring-B.fs").read_text() + "\n// B\n")
    for path in (f"/api/generate/{jid}/download?what=script", f"/api/generate/{jid}/download?what=zip",
                 f"/api/generate/{jid}/estimate"):
        assert call(web, "GET", path)[0] == 400
        assert call(web, "GET", path + ("&" if "?" in path else "?") + "variant=C")[0] == 400
    s, body = call(web, "GET", f"/api/generate/{jid}/download?what=script&variant=B")
    assert s == 200 and b"// B" in body
    s, body = call(web, "GET", f"/api/generate/{jid}/download?what=script&variant=A")
    assert s == 200 and b"// B" not in body
    assert call(web, "GET", f"/api/generate/{jid}/download?what=zip&variant=A")[1][:2] == b"PK"
    assert call(web, "GET", f"/api/generate/{jid}/estimate?variant=A")[0] == 200
    seen = []
    monkeypatch.setattr(pf, "push", lambda o, tr, name, text, source, **k: (seen.append((name, text)),
                        (PushResult(True, built=1, reused=0, url="u"), None))[1])
    assert call(web, "POST", f"/api/generate/{jid}/push")[0] == 400
    s, d = call(web, "POST", f"/api/generate/{jid}/push", {"variant": "B"})
    assert s == 200 and d["ok"] and seen[0][0] == "Ring-B" and "// B" in seen[0][1]


def test_variant_a_questions_stop_before_b(web, monkeypatch):
    fake = use_llm(monkeypatch, AskLLM())
    job = post(web, ask=True, variants=["", ""])
    r = job["result"]
    assert job["status"] == "done" and r["ok"] is False and r["variants"] == [] and r["questions"]
    assert len(fake.prompts) == 1


def test_variant_a_error_still_runs_b(web, monkeypatch):
    calls = []

    class Flaky(FakeLLM):
        def complete(self, *a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return super().complete(*a, **k)
    use_llm(monkeypatch, Flaky())
    job = post(web, variants=["", ""])
    a, b = job["result"]["variants"]
    assert job["status"] == "failed" and not a["ok"] and "boom" in a["error"] and b["ok"]


def test_questions_and_variants_validation(web):
    for extra in ({"answers": "x"}, {"answers": [{"question": "q", "answer": "a"}] * 5},
                  {"answers": [{"question": "q", "answer": "a" * 501}]}, {"answers": [{"question": "q"}]},
                  {"answers": ["x"]}, {"variants": ["a"]}, {"variants": ["a", "b", "c"]}, {"variants": "ab"},
                  {"variants": ["a", 3]}, {"variants": ["a", "b" * 301]}):
        assert call(web, "POST", "/api/generate", {"request": "a plate", **extra})[0] == 400, extra
