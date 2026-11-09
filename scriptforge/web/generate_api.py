"""Generate tab API: prompt -> part via `generate_native` in paste mode (0 Onshape calls), run in a background thread.

POST /api/generate               {request, name?, web_search?, max_fixes?, review_rounds?, images?: [upload ids],
                                  local_model?: installed Ollama model; forces backend "ollama" with it, no Claude calls,
                                  ask?: bool (the model may reply with clarifying questions: result.questions),
                                  answers?: [{question, answer}] (<= 4; appended to the request, ask is then off),
                                  variants?: [noteA, noteB] (make versions A and B in one job)} -> {"id"} (409 if busy)
GET  /api/generate/current       -> latest job summary or {"job": null}
GET  /api/generate/history?limit=N -> {"jobs": [light summaries, newest first]} from out/web/*/job.json plus running jobs
GET  /api/generate/{id}          -> the full job (as /current), also for finished jobs reloaded from their job.json
POST /api/generate/{id}/keep     {variant: "A"|"B"} -> the job; the winner's script becomes <name>.fs, the loser's is
                                 deleted; later push/download/estimate act on the winner without `variant`
POST /api/generate/{id}/revise   {change (1..2000 chars), variant?} -> {"id"}: a new job (parent = this one, name <name>-rev) that
                                 applies the change to the CURRENT saved script (409 if busy, like /api/generate)
GET  /api/generate/{id}/events   -> SSE: `log` {i, text} (from ?from=N), then `done` {status, result}
GET  /api/generate/{id}/download?what=script|stl|step|thumb|zip[&variant=A]   (variant is required for a job with variants; same for
                                 estimate (?variant=) and push (body {"variant"}))
                                 -> the saved .fs, the locally built STL/STEP/preview PNG (part_local.*, local_view_0.png; 0 Onshape calls), or a zip of the working files without the LLM logs (also for failed jobs)
GET  /api/generate/{id}/estimate -> {calls, text, mode, used, left, budget}: push estimate, 0 Onshape calls
POST /api/generate/{id}/push     -> push the saved script to Onshape (one at a time; refused over the run budget or the
                                 allocation left) -> {ok, url, built, reused, warnings, error, api_calls}
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path

from .paths import resolve_in_root
from .server import Download, HTTPError, route
from .uploads_api import local_model, upload_paths

MAX_LOG_LINES = 5000
MAX_JOBS = 10
MAX_REQUEST = 4000
MAX_ANSWER = 500
MAX_VARIANT = 300
MAX_CHANGE = 2000
NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
KEYS_HINT = "Onshape keys are needed even for local generation; set them in .env"


def sanitize_name(name, request: str) -> str:
    src = name if isinstance(name, str) and name.strip() else " ".join(re.findall(r"[A-Za-z0-9]+", request)[:5])
    out = NAME_RE.sub("-", src.strip()).strip("-_")[:60].strip("-_")
    return out or "part"


def _clamp(v, lo, hi, default) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return max(lo, min(hi, int(v)))


class Job:
    def __init__(self, request: str, name: str, images: int = 0, model: str | None = None,
                 prompt: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.prompt = prompt or request  # what the model gets: the request plus any answers
        self.request, self.name, self.images, self.model = request, name, images, model
        self.status = "running"
        self.started, self.finished = time.time(), None
        self.lines: list[str] = []
        self.dropped = 0  # lines discarded from the front once MAX_LOG_LINES is exceeded
        self.result: dict | None = None
        self.parent: str | None = None  # id of the job this one continues (reserved)
        self.kept: dict | None = None  # {"variant": "A", "script": "name.fs"} after "Keep this one"
        self.out_rel: str | None = None  # the job's output folder, relative to the root
        self.cond = threading.Condition()

    def log(self, text: str) -> None:
        with self.cond:
            for line in str(text).split("\n"):
                self.lines.append(line)
            if len(self.lines) > MAX_LOG_LINES:
                extra = len(self.lines) - MAX_LOG_LINES
                del self.lines[:extra]
                self.dropped += extra
            self.cond.notify_all()

    def finish(self, status: str, result: dict) -> None:
        with self.cond:
            self.status, self.result, self.finished = status, result, time.time()
            self.cond.notify_all()

    def summary(self) -> dict:
        with self.cond:
            return {"id": self.id, "request": self.request, "name": self.name, "images": self.images, "model": self.model, "status": self.status,
                    "started": self.started, "finished": self.finished, "result": self.result,
                    "log_lines": self.dropped + len(self.lines), "parent": self.parent, "kept": self.kept}

    def manifest(self) -> dict:
        with self.cond:
            return {"id": self.id, "name": self.name, "request": self.request, "prompt": self.prompt,
                    "model": self.model, "images": self.images, "started": self.started, "finished": self.finished,
                    "status": self.status, "result": self.result, "parent": self.parent, "kept": self.kept}

    @classmethod
    def from_manifest(cls, m: dict, out_rel: str) -> "Job":
        j = cls(str(m.get("request") or ""), str(m.get("name") or "part"), int(m.get("images") or 0),
                m.get("model"), m.get("prompt"))
        j.id = m["id"]
        j.started, j.finished = m.get("started") or 0, m.get("finished")
        j.status, j.result = m.get("status") or "failed", m.get("result")
        j.parent, j.kept, j.out_rel = m.get("parent"), m.get("kept"), out_rel
        if j.status == "running":  # the server stopped mid-run
            j.status = "failed"
            j.result = {"ok": False, "verified": False, "assumptions": None, "api_calls": 0, "script": None,
                        "out_dir": out_rel, "questions": [], "error": "interrupted (the server restarted)"}
        return j


_JOBS: list[Job] = []
_LOCK = threading.Lock()
_MANIFEST_LOCK = threading.Lock()
ID_RE = re.compile(r"[0-9a-f]{12}")
MANIFEST = "job.json"


def _write_manifest(root: Path, job: Job) -> None:
    """Atomically (temp file + replace) write out/web/<job>/job.json; never raises."""
    if not job.out_rel:
        return
    try:
        d = resolve_in_root(root, job.out_rel, suffixes=None)
        d.mkdir(parents=True, exist_ok=True)
        with _MANIFEST_LOCK:
            tmp = d / (MANIFEST + ".tmp")
            tmp.write_text(json.dumps(job.manifest(), indent=1))
            os.replace(tmp, d / MANIFEST)
    except Exception as e:  # noqa: BLE001  (history is best effort)
        job.log(f"warning: could not write {MANIFEST}: {e}")


def _manifests(root: Path):
    """Yield (relative folder, manifest dict) for readable out/web/*/job.json, newest folder first."""
    base = root / "out" / "web"
    if not base.is_dir():
        return
    for d in sorted((x for x in base.iterdir() if x.is_dir() and x.name != "uploads"), key=lambda x: x.name, reverse=True):
        try:
            m = json.loads((d / MANIFEST).read_text())
            if isinstance(m, dict) and isinstance(m.get("id"), str) and ID_RE.fullmatch(m["id"]):
                yield f"out/web/{d.name}", m
        except (OSError, ValueError):
            continue


def _load_job(root: Path, jid: str) -> Job | None:
    if not isinstance(jid, str) or not ID_RE.fullmatch(jid):
        return None
    for rel, m in _manifests(root):
        if m["id"] == jid:
            try:
                return Job.from_manifest(m, rel)
            except (TypeError, ValueError):
                return None
    return None


def _unique_script(root: Path, name: str) -> Path:
    n = 1
    while True:
        rel = f"{name}.fs" if n == 1 else f"{name}-{n}.fs"
        p = resolve_in_root(root, rel)
        if not p.exists():
            return p
        n += 1


def _one(job: Job, root: Path, out_dir: Path, opts: dict, images, name: str, ask: bool, note: str | None,
         prefix: str = "") -> dict:
    """One generate_native run -> the result dict (raises on unexpected errors)."""
    from ..native import generate as gen
    log = (lambda t: job.log(prefix + str(t))) if prefix else job.log
    common = dict(paste=True, budget=0, export_formats=(), web=opts["web_search"], max_fixes=opts["max_fixes"],
                  review_rounds=opts["review_rounds"], on_log=log, images=images,
                  **({"backend": "ollama", "model": job.model} if job.model else {}))
    if opts.get("revise"):
        rv = opts["revise"]
        res = gen.revise_native(rv["code"], job.request, out_dir, original_request=rv["original"], name=name, **common)
    else:
        res = gen.generate_native(job.prompt, out_dir, name=name, ask=ask, variant_note=note, **common)
    calls = res.get("api_calls") or 0
    if calls:
        log(f"WARNING: {calls} Onshape API call(s) were made; a local generation should make 0. Check usage.")
    questions = res.get("questions") or []
    result = {"ok": bool(res.get("ok")), "verified": bool(res.get("verified")),
              "assumptions": res.get("assumptions"), "error": res.get("error"), "api_calls": calls,
              "out_dir": out_dir.relative_to(root).as_posix(), "script": None, "questions": questions}
    src = out_dir / "part_studio.fs"
    if res.get("ok") and src.is_file():
        dest = _unique_script(root, name)
        shutil.copyfile(src, dest)
        result["script"] = dest.relative_to(root).as_posix()
        log(f"saved {result['script']}")
    elif not res.get("ok") and not result["error"] and not questions:
        result["error"] = "no verified script was produced; see the log (the last attempt is in the output folder)"
    return result


def _fail_result(root: Path, out_dir: Path, e: Exception, log) -> dict:
    msg = str(e)
    if "ONSHAPE_ACCESS_KEY" in msg:
        msg = KEYS_HINT
    log(f"error: {msg}")
    return {"ok": False, "verified": False, "assumptions": None, "error": msg, "api_calls": 0,
            "out_dir": out_dir.relative_to(root).as_posix() if out_dir.exists() else None, "script": None,
            "questions": []}


def _run(job: Job, root: Path, out_dir: Path, opts: dict, images: list[Path] = ()) -> None:
    try:
        _run_inner(job, root, out_dir, opts, images)
    finally:
        _write_manifest(root, job)


def _run_inner(job: Job, root: Path, out_dir: Path, opts: dict, images: list[Path]) -> None:
    from ..native.prompts import variant_note
    ask, notes = opts.get("ask", False), opts.get("variants")
    try:
        if not notes:
            result = _one(job, root, out_dir, opts, images, job.name, ask, None)
            job.finish("done" if result["ok"] or result["questions"] else "failed", result)
            return
        variants, a_res = [], None
        for i, label in enumerate("AB"):
            vdir, vname, prefix = out_dir / label, f"{job.name}-{label}", f"[{label}] "
            note = variant_note(label, notes[i], (a_res or {}).get("assumptions") if i else None)
            try:
                r = _one(job, root, vdir, opts, images, vname, ask and i == 0, note, prefix)
            except Exception as e:  # noqa: BLE001  (A failing must not stop B)
                r = _fail_result(root, vdir, e, lambda t: job.log(prefix + t))
            if r["questions"]:  # only A asks; B is not run
                job.finish("done", {**r, "variants": []})
                return
            if i == 0:
                a_res = r
            variants.append({"label": label, "note": note, **{k: r[k] for k in (
                "ok", "verified", "assumptions", "error", "script", "out_dir", "api_calls")}})
        ok = all(v["ok"] for v in variants)
        job.finish("done" if ok else "failed", {"ok": ok, "questions": [], "variants": variants,
                                                "api_calls": sum(v["api_calls"] for v in variants)})
    except Exception as e:  # noqa: BLE001
        job.finish("failed", _fail_result(root, out_dir, e, job.log))


def _answers(b) -> list[dict]:
    v = b.get("answers")
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > 4:
        raise HTTPError(400, "answers must be a list of at most 4 {question, answer} items")
    out = []
    for a in v:
        if (not isinstance(a, dict) or not isinstance(a.get("question"), str) or not isinstance(a.get("answer"), str)
                or len(a["question"]) > MAX_ANSWER or len(a["answer"]) > MAX_ANSWER):
            raise HTTPError(400, f"each answer needs a question and an answer of at most {MAX_ANSWER} characters")
        out.append({"question": a["question"], "answer": a["answer"]})
    return out


def _variant_notes(b):
    v = b.get("variants")
    if v is None:
        return None
    if (not isinstance(v, list) or len(v) != 2 or not all(isinstance(x, str) and len(x) <= MAX_VARIANT for x in v)):
        raise HTTPError(400, f"variants must be a list of exactly 2 notes of at most {MAX_VARIANT} characters")
    return [x.strip() for x in v]


@route("POST", "/api/generate")
def start(req):
    b = req.body
    if not isinstance(b, dict):
        raise HTTPError(400, "JSON body required")
    text = b.get("request")
    if not isinstance(text, str) or not text.strip():
        raise HTTPError(400, "request is required")
    text = text.strip()
    if len(text) > MAX_REQUEST:
        raise HTTPError(400, f"request is too long (max {MAX_REQUEST} characters)")
    answers = _answers(b)
    notes = _variant_notes(b)
    # questions already answered in the text (e.g. added from the photo reader) -> don't ask again
    ask = b.get("ask", False) is True and "Answers to your clarifying questions:" not in text
    if answers:
        from ..native.prompts import append_answers
        prompt = append_answers(text, answers)
        ask = False
    else:
        prompt = text
    name = sanitize_name(b.get("name"), text)
    ws = b.get("web_search", True)
    opts = {"web_search": ws if isinstance(ws, bool) else True,
            "max_fixes": _clamp(b.get("max_fixes"), 0, 8, 4), "review_rounds": _clamp(b.get("review_rounds"), 0, 3, 2),
            "ask": ask, "variants": notes}
    forced = b.get("local_model")
    model = local_model(forced) if forced not in (None, "") else None
    if model:
        opts["web_search"] = False
    root = req.app.root
    ids = b.get("images")
    images = upload_paths(root, ids) if ids else []
    with _LOCK:
        if any(j.status == "running" for j in _JOBS):
            raise HTTPError(409, "a generation is already running")
        job = Job(text, name, len(images), model, prompt)
        out_dir = resolve_in_root(root, f"out/web/{time.strftime('%Y%m%d-%H%M%S')}-{name}", suffixes=None)
        job.out_rel = out_dir.relative_to(root).as_posix()
        _JOBS.append(job)
        del _JOBS[:-MAX_JOBS]
    _write_manifest(root, job)
    threading.Thread(target=_run, args=(job, root, out_dir, opts, images), daemon=True, name=f"generate-{job.id}").start()
    return {"id": job.id}


def _original_request(root: Path, job: Job) -> str:
    """The first request (with answers) followed by every earlier change, so chained revisions keep the context."""
    changes: list[str] = []
    seen: set[str] = set()
    while job.parent and job.id not in seen and len(seen) < 20:
        seen.add(job.id)
        changes.append(job.request)
        with _LOCK:
            parent = next((j for j in _JOBS if j.id == job.parent), None)
        parent = parent or _load_job(root, job.parent)
        if parent is None:  # the first job is gone: the changes are all we have
            break
        job = parent
    parts = [] if job.parent else [job.prompt]
    return "\n\n".join(parts + [f"Change requested earlier: {c}" for c in reversed(changes)])


@route("POST", "/api/generate/{id}/revise")
def revise(req):
    from ..native.prompts import edit_prompt
    src = _job(req)
    b = req.body
    if not isinstance(b, dict):
        raise HTTPError(400, "JSON body required")
    change = b.get("change")
    if not isinstance(change, str) or not change.strip():
        raise HTTPError(400, "change is required")
    change = change.strip()
    if len(change) > MAX_CHANGE:
        raise HTTPError(400, f"change is too long (max {MAX_CHANGE} characters)")
    with src.cond:
        status = src.status
    if status == "running":
        raise HTTPError(409, "the generation is still running")
    res, base = _target(src, b.get("variant"))
    original = _original_request(req.app.root, src)
    code = _script_path(req, res).read_text()  # the saved file, which may hold the user's variable edits
    name = sanitize_name(base + "-rev", base)
    root = req.app.root
    opts = {"web_search": not src.model, "max_fixes": 8 if src.model else 4, "review_rounds": 2, "ask": False,
            "variants": None, "revise": {"code": code, "original": original}}
    with _LOCK:
        if any(j.status == "running" for j in _JOBS):
            raise HTTPError(409, "a generation is already running")
        job = Job(change, name, 0, src.model, edit_prompt(code, change, original))
        job.parent = src.id
        out_dir = resolve_in_root(root, f"out/web/{time.strftime('%Y%m%d-%H%M%S')}-{name}", suffixes=None)
        job.out_rel = out_dir.relative_to(root).as_posix()
        _JOBS.append(job)
        del _JOBS[:-MAX_JOBS]
    _write_manifest(root, job)
    threading.Thread(target=_run, args=(job, root, out_dir, opts), daemon=True, name=f"generate-{job.id}").start()
    return {"id": job.id}


@route("GET", "/api/generate/current")
def current(req):
    with _LOCK:
        job = _JOBS[-1] if _JOBS else None
    return {"job": job.summary() if job else None}


def _brief(job: Job) -> dict:
    s = job.summary()
    res = s["result"] or {}
    variants = res.get("variants") or []
    scripts = [v["script"] for v in variants if v.get("script")] if variants else ([res["script"]] if res.get("script") else [])
    return {"id": s["id"], "name": s["name"], "request": s["request"][:200], "status": s["status"],
            "started": s["started"], "finished": s["finished"], "ok": bool(res.get("ok")),
            "variants": [v["label"] for v in variants], "kept": s["kept"], "scripts": scripts, "parent": s["parent"]}


@route("GET", "/api/generate/history")
def history(req):
    try:
        limit = max(1, min(100, int(req.query.get("limit", 30))))
    except ValueError:
        raise HTTPError(400, "bad limit")
    with _LOCK:
        live = {j.id: j for j in _JOBS}
    jobs = dict(live)
    n = 0
    for rel, m in _manifests(req.app.root):
        if m["id"] not in jobs:
            try:
                jobs[m["id"]] = Job.from_manifest(m, rel)
            except (TypeError, ValueError):
                continue
        n += 1
        if n >= limit + len(live):
            break
    ordered = sorted(jobs.values(), key=lambda j: j.started or 0, reverse=True)[:limit]
    return {"jobs": [_brief(j) for j in ordered]}


@route("GET", "/api/generate/{id}")
def get_job(req):
    return _job(req).summary()


@route("GET", "/api/generate/{id}/events")
def events(req):
    job = _job(req)
    try:
        pos = max(0, int(req.query.get("from", 0)))
    except ValueError:
        raise HTTPError(400, "bad from")

    def gen():
        nonlocal pos
        while True:
            with job.cond:
                if pos - job.dropped >= len(job.lines) and job.status == "running":
                    job.cond.wait(timeout=10.0)
                pos = max(pos, job.dropped)
                batch = job.lines[pos - job.dropped:]
                start = pos
                pos += len(batch)
                status, result = job.status, job.result
            for i, text in enumerate(batch, start):
                yield "log", {"i": i, "text": text}
            if not batch:
                if status != "running":
                    yield "done", {"status": status, "result": result}
                    return
                yield "ping", {}  # lets the server notice a closed connection and stop this generator
    return gen()


# --- download / estimate / push of a finished job

_PUSH_LOCK = threading.Lock()


def _job(req) -> Job:
    jid = req.params["id"]
    with _LOCK:
        job = next((j for j in _JOBS if j.id == jid), None)
    if job is None:
        job = _load_job(req.app.root, jid)  # finished job from an earlier server run
    if job is None:
        raise HTTPError(404, "unknown job")
    return job


def _target(job: Job, label) -> tuple[dict, str]:
    """(result dict, name) to act on: the job's, or the chosen variant's (required when the job has variants)."""
    with job.cond:
        res = job.result or {}
    variants = res.get("variants") or []
    if not variants:
        return res, job.name
    with job.cond:
        kept = job.kept
    if kept:  # the winner stands in for the job
        v = next((x for x in variants if x["label"] == kept["variant"]), None)
        if v is not None and label in (None, "", kept["variant"]):
            return v, job.name
        raise HTTPError(400, f"version {kept['variant']} was kept; the other version is gone")
    v = next((x for x in variants if x["label"] == label), None) if isinstance(label, str) else None
    if v is None:
        raise HTTPError(400, "variant is required: one of " + ", ".join(x["label"] for x in variants))
    return v, f"{job.name}-{v['label']}"


def _script_path(req, res: dict) -> Path:
    rel = res.get("script")
    if not rel:
        raise HTTPError(404, "this job has no saved script")
    p = resolve_in_root(req.app.root, rel)
    if not p.is_file():
        raise HTTPError(404, "the saved script is gone")
    return p


def _run_budget() -> int:
    try:
        return int(os.environ.get("ONSHAPE_RUN_BUDGET") or 60)
    except ValueError:
        return 60


def _estimate(req, res: dict, name: str):
    """-> (trace, script text, Estimate, mode, Allocation); 0 Onshape calls."""
    from ..native import pushflow
    from ..native.script import trace
    from ..usage import allocation_status
    text = _script_path(req, res).read_text()
    tr = trace(text)
    if not tr.ok:
        raise HTTPError(409, f"script error: {tr.error}")
    est, st0 = pushflow.estimate(tr, name)
    return tr, text, est, "new" if st0 is None else "update", allocation_status()


@route("POST", "/api/generate/{id}/keep")
def keep(req):
    job = _job(req)
    b = req.body if isinstance(req.body, dict) else {}
    label = b.get("variant")
    root = req.app.root
    with job.cond:
        status, res, kept = job.status, job.result or {}, job.kept
    variants = res.get("variants") or []
    if status == "running":
        raise HTTPError(409, "the generation is still running")
    if not variants:
        raise HTTPError(400, "this job has no versions to choose from")
    if label not in ("A", "B"):
        raise HTTPError(400, "variant must be A or B")
    if kept:
        raise HTTPError(400, f"version {kept['variant']} was already kept")
    win = next((v for v in variants if v["label"] == label), None)
    if win is None or not win.get("ok") or not win.get("script"):
        raise HTTPError(400, f"version {label} did not succeed; it cannot be kept")
    lose = next((v for v in variants if v["label"] != label), None)
    src = resolve_in_root(root, win["script"])
    if not src.is_file():
        raise HTTPError(404, "the saved script is gone")
    with _LOCK:  # one rename at a time so two keeps cannot pick the same free name
        dest = _unique_script(root, job.name)
        src.rename(dest)
    new = dest.relative_to(root).as_posix()
    if lose and lose.get("script"):
        try:
            resolve_in_root(root, lose["script"]).unlink(missing_ok=True)
        except OSError:
            pass
    with job.cond:
        win["script"] = new
        if lose:
            lose["script"] = None
        job.kept = {"variant": label, "script": new}
    _write_manifest(root, job)
    return job.summary()


# what= -> (file in the job's out_dir, download extension, content type)
_BUILT = {"stl": ("part_local.stl", "stl", "model/stl"), "step": ("part_local.step", "step", "model/step"),
          "thumb": ("local_view_0.png", "png", "image/png")}  # thumb: the newest local_view_N.png


@route("GET", "/api/generate/{id}/download")
def download(req):
    job = _job(req)
    with job.cond:
        status = job.status
    if status == "running":
        raise HTTPError(409, "the generation is still running")
    result, name = _target(job, req.query.get("variant"))
    what = req.query.get("what", "script")
    if what == "script":
        p = _script_path(req, result)
        return Download(p.read_bytes(), f"{name}.fs", "text/plain; charset=utf-8")
    if what not in ("zip", *_BUILT):
        raise HTTPError(400, "what must be script, stl, step, thumb or zip")
    rel = result.get("out_dir")
    if not rel:
        raise HTTPError(404, "no working files for this job")
    out = resolve_in_root(req.app.root, rel, suffixes=None)
    if not out.is_dir():
        raise HTTPError(404, "the working files are gone")
    if what in _BUILT:  # files the local build already saved: no rebuild, no Onshape calls
        fname, ext, ctype = _BUILT[what]
        f = out / fname
        if what == "thumb":  # the last review render shows the final geometry
            views = sorted(out.glob("local_view_*.png"), key=lambda p: int(p.stem.rsplit("_", 1)[1])
                           if p.stem.rsplit("_", 1)[1].isdigit() else -1)
            f = views[-1] if views else f
        if not f.is_file():
            raise HTTPError(404, f"no {what} was built for this job (the part did not build locally)")
        return Download(f.read_bytes(), f"{name}.{ext}", ctype)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file() and not (f.parent == out and f.name.startswith("llm_") and f.suffix == ".md"):
                z.write(f, f.relative_to(out).as_posix())
        if result.get("script"):
            z.write(_script_path(req, result), f"{name}.fs")
    return Download(buf.getvalue(), f"{name}.zip", "application/zip")


@route("GET", "/api/generate/{id}/estimate")
def estimate(req):
    job = _job(req)
    res, name = _target(job, req.query.get("variant"))
    _tr, _text, est, mode, alloc = _estimate(req, res, name)
    return {"calls": est.total, "text": est.text(alloc), "mode": mode, "used": alloc.used, "left": alloc.left,
            "budget": _run_budget(), "warnings": list(est.warnings)}


@route("POST", "/api/generate/{id}/push")
def push(req):
    from ..native import pushflow
    from ..onshape import Onshape, OnshapeError
    job = _job(req)
    res, name = _target(job, req.body.get("variant") if isinstance(req.body, dict) else None)
    tr, text, est, _mode, alloc = _estimate(req, res, name)
    if not (os.environ.get("ONSHAPE_ACCESS_KEY") and os.environ.get("ONSHAPE_SECRET_KEY")):
        raise HTTPError(400, "Onshape keys are needed to push; set them in .env")
    budget = _run_budget()
    if est.total > budget:
        raise HTTPError(409, f"estimate {est.total} exceeds the run budget {budget} (ONSHAPE_RUN_BUDGET)")
    if est.total > alloc.left:
        raise HTTPError(409, f"estimate {est.total} exceeds the {alloc.left} API calls left")
    if not _PUSH_LOCK.acquire(blocking=False):
        raise HTTPError(409, "another push is already running")
    o = None
    try:
        o = Onshape(budget=budget, purpose=f"web push {name}")
        r, _st = pushflow.push(o, tr, name, text, _script_path(req, res).as_posix())
        return {"ok": bool(r.ok), "url": r.url, "built": r.built, "reused": r.reused, "warnings": list(r.warnings),
                "error": r.error, "api_calls": o.calls}
    except Exception as e:  # noqa: BLE001  (Onshape errors, budget stops, ...: a message, never a stack trace)
        msg = str(e) if isinstance(e, OnshapeError) else f"{type(e).__name__}: {e}"
        return {"ok": False, "error": msg, "url": None, "built": 0, "reused": 0, "warnings": [],
                "api_calls": o.calls if o else 0}
    finally:
        _PUSH_LOCK.release()
