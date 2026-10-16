"""Generate tab API: prompt -> part via `generate_native` in paste mode (0 Onshape calls), run in a background thread.

POST /api/generate               {request, name?, web_search?, max_fixes?, review_rounds?, images?: [upload ids]} -> {"id"} (409 if busy)
GET  /api/generate/current       -> latest job summary or {"job": null}
GET  /api/generate/{id}/events   -> SSE: `log` {i, text} (from ?from=N), then `done` {status, result}
"""
from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from .paths import resolve_in_root
from .server import HTTPError, route
from .uploads_api import upload_paths

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
    def __init__(self, request: str, name: str, images: int = 0):
        self.id = uuid.uuid4().hex[:12]
        self.request, self.name, self.images = request, name, images
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
            return {"id": self.id, "request": self.request, "name": self.name, "images": self.images, "status": self.status,
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
                                  review_rounds=opts["review_rounds"], on_log=job.log, images=images)
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
    root = req.app.root
    ids = b.get("images")
    images = upload_paths(root, ids) if ids else []
    with _LOCK:
        if any(j.status == "running" for j in _JOBS):
            raise HTTPError(409, "a generation is already running")
        job = Job(text, name, len(images))
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
