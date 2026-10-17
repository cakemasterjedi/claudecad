"""Generate tab API: prompt -> part via `generate_native` in paste mode (0 Onshape calls), run in a background thread.

POST /api/generate               {request, name?, web_search?, max_fixes?, review_rounds?, images?: [upload ids],
                                  local_model?: installed Ollama model; forces backend "ollama" with it, no Claude calls} -> {"id"} (409 if busy)
GET  /api/generate/current       -> latest job summary or {"job": null}
GET  /api/generate/{id}/events   -> SSE: `log` {i, text} (from ?from=N), then `done` {status, result}
GET  /api/generate/{id}/download?what=script|zip
                                 -> the saved .fs, or a zip of the working files without the LLM logs (also for failed jobs)
GET  /api/generate/{id}/estimate -> {calls, text, mode, used, left, budget}: push estimate, 0 Onshape calls
POST /api/generate/{id}/push     -> push the saved script to Onshape (one at a time; refused over the run budget or the
                                 allocation left) -> {ok, url, built, reused, warnings, error, api_calls}
"""
from __future__ import annotations

import io
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
    def __init__(self, request: str, name: str, images: int = 0, model: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.request, self.name, self.images, self.model = request, name, images, model
        self.status = "running"
        self.started, self.finished = time.time(), None
        self.lines: list[str] = []
        self.dropped = 0  # lines discarded from the front once MAX_LOG_LINES is exceeded
        self.result: dict | None = None
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
                    "log_lines": self.dropped + len(self.lines)}


_JOBS: list[Job] = []
_LOCK = threading.Lock()


def _unique_script(root: Path, name: str) -> Path:
    n = 1
    while True:
        rel = f"{name}.fs" if n == 1 else f"{name}-{n}.fs"
        p = resolve_in_root(root, rel)
        if not p.exists():
            return p
        n += 1


def _run(job: Job, root: Path, out_dir: Path, opts: dict, images: list[Path] = ()) -> None:
    from ..native import generate as gen
    try:
        res = gen.generate_native(job.request, out_dir, name=job.name, paste=True, budget=0, export_formats=(),
                                  web=opts["web_search"], max_fixes=opts["max_fixes"],
                                  review_rounds=opts["review_rounds"], on_log=job.log, images=images,
                                  **({"backend": "ollama", "model": job.model} if job.model else {}))
        calls = res.get("api_calls") or 0
        if calls:
            job.log(f"WARNING: {calls} Onshape API call(s) were made; a local generation should make 0. Check usage.")
        result = {"ok": bool(res.get("ok")), "verified": bool(res.get("verified")),
                  "assumptions": res.get("assumptions"), "error": res.get("error"), "api_calls": calls,
                  "out_dir": out_dir.relative_to(root).as_posix(), "script": None}
        src = out_dir / "part_studio.fs"
        if res.get("ok") and src.is_file():
            dest = _unique_script(root, job.name)
            shutil.copyfile(src, dest)
            result["script"] = dest.relative_to(root).as_posix()
            job.log(f"saved {result['script']}")
        elif not res.get("ok") and not result["error"]:
            result["error"] = "no verified script was produced; see the log (the last attempt is in the output folder)"
        job.finish("done" if result["ok"] else "failed", result)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "ONSHAPE_ACCESS_KEY" in msg:
            msg = KEYS_HINT
        job.log(f"error: {msg}")
        job.finish("failed", {"ok": False, "verified": False, "assumptions": None, "error": msg, "api_calls": 0,
                              "out_dir": out_dir.relative_to(root).as_posix() if out_dir.exists() else None,
                              "script": None})


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
    name = sanitize_name(b.get("name"), text)
    ws = b.get("web_search", True)
    opts = {"web_search": ws if isinstance(ws, bool) else True,
            "max_fixes": _clamp(b.get("max_fixes"), 0, 8, 4), "review_rounds": _clamp(b.get("review_rounds"), 0, 3, 2)}
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
        job = Job(text, name, len(images), model)
        out_dir = resolve_in_root(root, f"out/web/{time.strftime('%Y%m%d-%H%M%S')}-{name}", suffixes=None)
        _JOBS.append(job)
        del _JOBS[:-MAX_JOBS]
    threading.Thread(target=_run, args=(job, root, out_dir, opts, images), daemon=True, name=f"generate-{job.id}").start()
    return {"id": job.id}


@route("GET", "/api/generate/current")
def current(req):
    with _LOCK:
        job = _JOBS[-1] if _JOBS else None
    return {"job": job.summary() if job else None}


@route("GET", "/api/generate/{id}/events")
def events(req):
    with _LOCK:
        job = next((j for j in _JOBS if j.id == req.params["id"]), None)
    if job is None:
        raise HTTPError(404, "unknown job")
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
    with _LOCK:
        job = next((j for j in _JOBS if j.id == req.params["id"]), None)
    if job is None:
        raise HTTPError(404, "unknown job")
    return job


def _script_path(req, job: Job) -> Path:
    rel = (job.result or {}).get("script")
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


def _estimate(req, job: Job):
    """-> (trace, script text, Estimate, mode, Allocation); 0 Onshape calls."""
    from ..native import pushflow
    from ..native.script import trace
    from ..usage import allocation_status
    text = _script_path(req, job).read_text()
    tr = trace(text)
    if not tr.ok:
        raise HTTPError(409, f"script error: {tr.error}")
    est, st0 = pushflow.estimate(tr, job.name)
    return tr, text, est, "new" if st0 is None else "update", allocation_status()


@route("GET", "/api/generate/{id}/download")
def download(req):
    job = _job(req)
    with job.cond:
        status, result = job.status, job.result
    if status == "running":
        raise HTTPError(409, "the generation is still running")
    what = req.query.get("what", "script")
    if what == "script":
        p = _script_path(req, job)
        return Download(p.read_bytes(), f"{job.name}.fs", "text/plain; charset=utf-8")
    if what != "zip":
        raise HTTPError(400, "what must be script or zip")
    rel = (result or {}).get("out_dir")
    if not rel:
        raise HTTPError(404, "no working files for this job")
    out = resolve_in_root(req.app.root, rel, suffixes=None)
    if not out.is_dir():
        raise HTTPError(404, "the working files are gone")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file() and not (f.parent == out and f.name.startswith("llm_") and f.suffix == ".md"):
                z.write(f, f.relative_to(out).as_posix())
        if (result or {}).get("script"):
            z.write(_script_path(req, job), f"{job.name}.fs")
    return Download(buf.getvalue(), f"{job.name}.zip", "application/zip")


@route("GET", "/api/generate/{id}/estimate")
def estimate(req):
    job = _job(req)
    _tr, _text, est, mode, alloc = _estimate(req, job)
    return {"calls": est.total, "text": est.text(alloc), "mode": mode, "used": alloc.used, "left": alloc.left,
            "budget": _run_budget()}


@route("POST", "/api/generate/{id}/push")
def push(req):
    from ..native import pushflow
    from ..onshape import Onshape, OnshapeError
    job = _job(req)
    tr, text, est, _mode, alloc = _estimate(req, job)
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
        o = Onshape(budget=budget, purpose=f"web push {job.name}")
        r, _st = pushflow.push(o, tr, job.name, text, _script_path(req, job).as_posix())
        return {"ok": bool(r.ok), "url": r.url, "built": r.built, "reused": r.reused, "warnings": list(r.warnings),
                "error": r.error, "api_calls": o.calls}
    except Exception as e:  # noqa: BLE001  (Onshape errors, budget stops, ...: a message, never a stack trace)
        msg = str(e) if isinstance(e, OnshapeError) else f"{type(e).__name__}: {e}"
        return {"ok": False, "error": msg, "url": None, "built": 0, "reused": 0, "warnings": [],
                "api_calls": o.calls if o else 0}
    finally:
        _PUSH_LOCK.release()
