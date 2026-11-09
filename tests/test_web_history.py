"""Job history (job.json manifests) and Keep A / Keep B."""
import json
import os
import time

import scriptforge.web.generate_api as ga
from tests.test_web_generate import FakeLLM, call, post, use_llm, web, _login, wait_done  # noqa: F401


def manifest(web, job):
    return json.loads((web.root / job["result"]["out_dir"] / "job.json").read_text())


def test_manifest_written_at_start_and_finish(web, monkeypatch):
    import threading
    gate = threading.Event()
    use_llm(monkeypatch, FakeLLM(gate))
    s, d = call(web, "POST", "/api/generate", {"request": "a plate", "name": "Mf", "review_rounds": 0})
    assert s == 200
    (m,) = list((web.root / "out/web").glob("*-Mf/job.json"))
    start = json.loads(m.read_text())
    assert start["id"] == d["id"] and start["status"] == "running" and start["finished"] is None
    assert start["parent"] is None and start["kept"] is None and start["request"] == "a plate"
    gate.set()
    job = wait_done(web)
    end = manifest(web, job)
    assert end["status"] == "done" and end["finished"] and end["result"]["ok"] and end["result"]["script"] == "Mf.fs"
    assert not list(m.parent.glob("*.tmp"))


def test_history_order_limit_and_skips(web, monkeypatch):
    use_llm(monkeypatch, FakeLLM())
    ids = []
    for n in ("One", "Two", "Three"):
        post(web, name=n)
        ids.append(wait_done(web)["id"])
        time.sleep(1.1)  # folder names carry second resolution
    base = web.root / "out/web"
    (base / "uploads").mkdir(exist_ok=True)
    (base / "uploads" / "job.json").write_text(json.dumps({"id": "0" * 12}))
    (base / "zz-corrupt").mkdir()
    (base / "zz-corrupt" / "job.json").write_text("{not json")
    (base / "zz-nomanifest").mkdir()
    ga._JOBS.clear()
    s, d = call(web, "GET", "/api/generate/history")
    assert s == 200
    assert [j["id"] for j in d["jobs"]] == ids[::-1]
    j = d["jobs"][0]
    assert j["name"] == "Three" and j["status"] == "done" and j["ok"] and j["scripts"] == ["Three.fs"] and j["kept"] is None
    assert [j["id"] for j in call(web, "GET", "/api/generate/history?limit=2")[1]["jobs"]] == ids[::-1][:2]
    assert len(call(web, "GET", "/api/generate/history?limit=1000")[1]["jobs"]) == 3
    assert call(web, "GET", "/api/generate/history?limit=x")[0] == 400


def test_history_running_job_wins_over_disk(web, monkeypatch):
    import threading
    gate = threading.Event()
    use_llm(monkeypatch, FakeLLM(gate))
    call(web, "POST", "/api/generate", {"request": "r" * 300, "review_rounds": 0})
    d = call(web, "GET", "/api/generate/history")[1]
    assert len(d["jobs"]) == 1 and d["jobs"][0]["status"] == "running" and len(d["jobs"][0]["request"]) == 200
    gate.set()
    wait_done(web)


def test_job_reloaded_from_disk_after_restart(web, monkeypatch):
    use_llm(monkeypatch, FakeLLM())
    job = post(web, name="Disk")
    jid = job["id"]
    ga._JOBS.clear()
    s, d = call(web, "GET", f"/api/generate/{jid}")
    assert s == 200 and d["id"] == jid and d["result"]["script"] == "Disk.fs" and d["status"] == "done"
    ga._JOBS.clear()
    assert call(web, "GET", f"/api/generate/{jid}/download?what=script")[0] == 200
    assert call(web, "GET", f"/api/generate/{jid}/download?what=zip")[1][:2] == b"PK"
    assert call(web, "GET", f"/api/generate/{jid}/estimate")[0] == 200
    from tests.test_web_generate import events
    ev = events(web, jid)
    assert ev[-1][0] == "done" and ev[-1][1]["status"] == "done"


def test_bad_ids_rejected(web):
    for jid in ("nope", "../../etc", "..", "0" * 12, "%2e%2e", "A" * 12, "g" * 12):
        assert call(web, "GET", f"/api/generate/{jid}")[0] == 404
        assert call(web, "POST", f"/api/generate/{jid}/keep", {"variant": "A"})[0] == 404
        assert call(web, "GET", f"/api/generate/{jid}/estimate")[0] == 404


def test_running_manifest_after_restart_is_failed(web):
    d = web.root / "out/web/20200101-000000-x"
    d.mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({"id": "a" * 12, "name": "x", "request": "r", "status": "running",
                                            "started": 1.0, "finished": None, "result": None}))
    s, j = call(web, "GET", "/api/generate/" + "a" * 12)
    assert s == 200 and j["status"] == "failed" and "interrupted" in j["result"]["error"]


# --- keep

def two(web, monkeypatch, name="Ring"):
    use_llm(monkeypatch, FakeLLM())
    job = post(web, name=name, variants=["", ""])
    assert job["status"] == "done"
    return job["id"]


def test_keep_happy_path(web, monkeypatch):
    import scriptforge.native.pushflow as pf
    from scriptforge.native.build import PushResult
    jid = two(web, monkeypatch)
    a_text = (web.root / "Ring-A.fs").read_text()
    s, d = call(web, "POST", f"/api/generate/{jid}/keep", {"variant": "A"})
    assert s == 200 and d["kept"] == {"variant": "A", "script": "Ring.fs"}
    assert (web.root / "Ring.fs").read_text() == a_text
    assert not (web.root / "Ring-A.fs").exists() and not (web.root / "Ring-B.fs").exists()
    assert len(list((web.root / "out/web").glob("*-Ring/[AB]"))) == 2
    m = json.loads(next((web.root / "out/web").glob("*-Ring/job.json")).read_text())
    assert m["kept"] == {"variant": "A", "script": "Ring.fs"}
    for jobs in ("memory", "disk"):
        if jobs == "disk":
            ga._JOBS.clear()
        assert call(web, "GET", f"/api/generate/{jid}/download?what=script")[0] == 200
        assert call(web, "GET", f"/api/generate/{jid}/estimate")[0] == 200
        assert call(web, "GET", f"/api/generate/{jid}/estimate?variant=A")[0] == 200
        assert call(web, "GET", f"/api/generate/{jid}/estimate?variant=B")[0] == 400
    assert call(web, "GET", "/api/generate/history")[1]["jobs"][0]["kept"]["variant"] == "A"
    seen = []
    monkeypatch.setattr(pf, "push", lambda o, tr, name, text, source, **k: (seen.append(name),
                        (PushResult(True, built=1, reused=0, url="u"), None))[1])
    s, d = call(web, "POST", f"/api/generate/{jid}/push")
    assert s == 200 and d["ok"] and seen == ["Ring"]


def test_keep_400_and_404_cases(web, monkeypatch):
    jid = two(web, monkeypatch)
    url = f"/api/generate/{jid}/keep"
    assert call(web, "POST", url)[0] == 400
    assert call(web, "POST", url, {"variant": "C"})[0] == 400
    assert call(web, "POST", url, {"variant": 1})[0] == 400
    assert call(web, "POST", "/api/generate/" + "f" * 12 + "/keep", {"variant": "A"})[0] == 404
    assert call(web, "POST", url, {"variant": "B"})[0] == 200
    assert call(web, "POST", url, {"variant": "B"})[0] == 400  # already kept
    assert call(web, "POST", url, {"variant": "A"})[0] == 400
    # non-variant job
    single = post(web, name="Solo")
    assert call(web, "POST", f"/api/generate/{single['id']}/keep", {"variant": "A"})[0] == 400


def test_keep_failed_variant_and_missing_loser(web, monkeypatch):
    jid = two(web, monkeypatch)
    job = ga._JOBS[-1]
    job.result["variants"][0]["ok"] = False
    assert call(web, "POST", f"/api/generate/{jid}/keep", {"variant": "A"})[0] == 400
    (web.root / "Ring-A.fs").unlink()  # loser already gone: just skip deleting it
    s, d = call(web, "POST", f"/api/generate/{jid}/keep", {"variant": "B"})
    assert s == 200 and d["kept"]["script"] == "Ring.fs" and (web.root / "Ring.fs").is_file()


def test_keep_does_not_overwrite_existing(web, monkeypatch):
    (web.root / "Ring.fs").write_text("mine")
    jid = two(web, monkeypatch)
    s, d = call(web, "POST", f"/api/generate/{jid}/keep", {"variant": "B"})
    assert s == 200 and d["kept"]["script"] == "Ring-2.fs"
    assert (web.root / "Ring.fs").read_text() == "mine" and (web.root / "Ring-2.fs").is_file()


# --- revise

def test_edit_prompt_contents():
    from scriptforge.native.prompts import edit_prompt
    p = edit_prompt("CODE_HERE", "make it thicker", "a plate 80 x 50")
    assert "CODE_HERE" in p and "make it thicker" in p and "a plate 80 x 50" in p
    assert "complete" in p and "featurescript" in p and "variable names" in p
    assert "Original design request" not in edit_prompt("c", "x")


def revise_ok(web, jid, **body):
    s, d = call(web, "POST", f"/api/generate/{jid}/revise", body)
    assert s == 200, d
    return wait_done(web)


def test_revise_happy_path(web, monkeypatch):
    fake = use_llm(monkeypatch, FakeLLM())
    src = post(web, name="Plate", request="a mounting plate")
    path = web.root / "Plate.fs"
    original = path.read_text()
    edited = original + "\n// user edited variable\n"
    path.write_text(edited)  # simulates saved variable edits
    fake.prompts.clear()
    job = revise_ok(web, src["id"], change="add a lid lip")
    assert job["status"] == "done" and job["parent"] == src["id"] and job["request"] == "add a lid lip"
    assert job["name"] == "Plate-rev" and job["result"]["script"] == "Plate-rev.fs"
    assert (web.root / "Plate-rev.fs").is_file() and path.read_text() == edited
    assert "// user edited variable" in fake.prompts[0] and "add a lid lip" in fake.prompts[0]
    assert "a mounting plate" in fake.prompts[0]
    out = web.root / job["result"]["out_dir"]
    assert "add a lid lip" in (out / "request.txt").read_text()
    m = json.loads((out / "job.json").read_text())
    assert m["parent"] == src["id"]
    # a second revision never overwrites
    job2 = revise_ok(web, job["id"], change="more")
    assert job2["result"]["script"] == "Plate-rev-rev.fs"
    job3 = revise_ok(web, src["id"], change="again")
    assert job3["result"]["script"] == "Plate-rev-2.fs"
    h = call(web, "GET", "/api/generate/history")[1]["jobs"]
    assert h[0]["parent"] == src["id"]


def test_revise_variant_kept_and_disk(web, monkeypatch):
    jid = two(web, monkeypatch)
    fake = use_llm(monkeypatch, FakeLLM())
    s, d = call(web, "POST", f"/api/generate/{jid}/revise", {"change": "x"})
    assert s == 400  # variant required
    (web.root / "Ring-B.fs").write_text((web.root / "Ring-B.fs").read_text() + "\n// B only\n")
    fake.prompts.clear()
    job = revise_ok(web, jid, change="x", variant="B")
    assert job["name"] == "Ring-B-rev" and "// B only" in fake.prompts[0]
    assert call(web, "POST", f"/api/generate/{jid}/keep", {"variant": "B"})[0] == 200
    ga._JOBS.clear()  # reloaded from disk
    fake.prompts.clear()
    job = revise_ok(web, jid, change="y")
    assert job["name"] == "Ring-rev" and job["parent"] == jid and "// B only" in fake.prompts[0]
    assert job["result"]["script"] == "Ring-rev.fs"


def test_revise_errors(web, monkeypatch):
    import threading
    use_llm(monkeypatch, FakeLLM())
    src = post(web, name="Err")
    url = f"/api/generate/{src['id']}/revise"
    for body in (None, {}, {"change": ""}, {"change": "  "}, {"change": 5}, {"change": "x" * 2001}):
        assert call(web, "POST", url, body)[0] == 400
    assert call(web, "POST", "/api/generate/" + "e" * 12 + "/revise", {"change": "x"})[0] == 404
    assert call(web, "POST", "/api/generate/nope/revise", {"change": "x"})[0] == 404
    (web.root / "Err.fs").unlink()
    assert call(web, "POST", url, {"change": "x"})[0] == 404  # script gone
    gate = threading.Event()
    use_llm(monkeypatch, FakeLLM(gate))
    (web.root / "Err.fs").write_text("x")
    call(web, "POST", "/api/generate", {"request": "other", "review_rounds": 0})
    assert call(web, "POST", url, {"change": "x"})[0] == 409
    running = ga._JOBS[-1]
    assert call(web, "POST", f"/api/generate/{running.id}/revise", {"change": "x"})[0] == 409
    gate.set()
    wait_done(web)


def test_original_request_keeps_the_whole_revision_chain(tmp_path, monkeypatch):
    from scriptforge.web import generate_api as g
    a = g.Job("make a ring", "ring", 0, None, "make a ring\nAnswers: gold")
    b = g.Job("thicker", "ring-rev", 0, None, "x")
    b.parent = a.id
    c = g.Job("add gem", "ring-rev-rev", 0, None, "y")
    c.parent = b.id
    monkeypatch.setattr(g, "_JOBS", [a, b, c])
    assert g._original_request(tmp_path, c) == ("make a ring\nAnswers: gold\n\nChange requested earlier: thicker"
                                                "\n\nChange requested earlier: add gem")
    assert g._original_request(tmp_path, a) == "make a ring\nAnswers: gold"
    monkeypatch.setattr(g, "_JOBS", [b, c])  # first job gone: only the changes remain
    assert g._original_request(tmp_path, c) == "Change requested earlier: thicker\n\nChange requested earlier: add gem"
