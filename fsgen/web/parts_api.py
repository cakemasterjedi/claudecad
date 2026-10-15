"""Parts tab API: list, read, save and create scripts under the project root; check a script (0 Onshape calls)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .checkworker import CheckCrashed, CheckTimeout, check_in_worker
from .paths import list_scripts, resolve_in_root
from .server import HTTPError, route

MAX_TEXT = 1_000_000
MTIME_TOLERANCE = 1e-6  # seconds; mtimes travel as JSON floats


def _mtime(p: Path) -> float:
    return p.stat().st_mtime


def _text(body) -> str:
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        raise HTTPError(400, "text (string) required")
    if len(text.encode()) > MAX_TEXT:
        raise HTTPError(413, "script too large (max 1 MB)")
    return text


def _atomic_write(p: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".fsgen-", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if p.exists():
            os.chmod(tmp, p.stat().st_mode & 0o777)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rel(req, p: Path) -> str:
    return p.relative_to(req.app.root.resolve()).as_posix()


@route("GET", "/api/scripts")
def scripts(req):
    return {"scripts": list_scripts(req.app.root)}


@route("GET", "/api/scripts/{rest:path}")
def read_script(req):
    p = resolve_in_root(req.app.root, req.params["rest"])
    if not p.is_file():
        raise HTTPError(404, "no such script")
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPError(415, "script is not UTF-8 text")
    return {"path": _rel(req, p), "text": text, "mtime": _mtime(p)}


@route("PUT", "/api/scripts/{rest:path}")
def save_script(req):
    p = resolve_in_root(req.app.root, req.params["rest"])
    text = _text(req.body)
    if not p.is_file():
        raise HTTPError(404, "no such script (use POST /api/scripts to create it)")
    sent = req.body.get("mtime")
    if isinstance(sent, bool) or not isinstance(sent, (int, float)):
        raise HTTPError(400, "mtime (number) required")
    if abs(_mtime(p) - sent) > MTIME_TOLERANCE:
        return 409, {"error": "changed on disk", "mtime": _mtime(p)}
    _atomic_write(p, text)
    return {"path": _rel(req, p), "mtime": _mtime(p)}


@route("POST", "/api/scripts")
def create_script(req):
    body = req.body if isinstance(req.body, dict) else {}
    p = resolve_in_root(req.app.root, body.get("path"))
    text = _text(body)
    if p.exists():
        return 409, {"error": "already exists"}
    root = req.app.root.resolve()
    if not p.parent.is_relative_to(root):
        raise HTTPError(403, "path outside the project root")
    p.parent.mkdir(parents=True, exist_ok=True)  # inside root only (checked above)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)  # exclusive: never clobbers a racing create
    except FileExistsError:
        return 409, {"error": "already exists"}
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return {"path": _rel(req, p), "mtime": _mtime(p)}


@route("POST", "/api/check")
def check(req):
    body = req.body if isinstance(req.body, dict) else {}
    p = resolve_in_root(req.app.root, body.get("path"))
    if not p.is_file():
        raise HTTPError(404, "no such script")
    mw = body.get("min_wall", 1.2)
    if isinstance(mw, bool) or not isinstance(mw, (int, float)) or mw != mw:
        mw = 1.2
    mw = min(10.0, max(0.1, float(mw)))
    try:
        return check_in_worker(p, mw)
    except CheckTimeout as e:
        raise HTTPError(504, "check timed out" if "timed out" in str(e) else str(e))
    except CheckCrashed as e:
        raise HTTPError(500, str(e))
