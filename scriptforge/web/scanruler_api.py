"""ScanRuler hand-off: scan storage, project archives and updater control.

PUT    /api/scans?name=<file>   raw body (stl/ply/obj/3mf, max 500 MB) -> {"id", "name", "size", "uploaded"} (415/413)
GET    /api/scans               -> {"scans": [{id, name, size, uploaded}, ...]} newest first (20 kept)
DELETE /api/scans/{id}          -> {"ok": true} (404)
GET    /api/scans/{id}/file/{filename}                         the stored scan bytes (filename must match)
GET    /api/scans/{id}/check/{jobid}/{variant}/check.scanruler a ScanRuler project: this scan vs the job's built STL
                                (variant "-" = none/default; 404 when the job has no built STL)
GET    /api/scanruler           -> {installed, version, latest, checked_at, updating, error}
POST   /api/scanruler/update    -> {"started": true} (409 when already running)
Scans live in <root>/scans/<id>/<name>.
"""
from __future__ import annotations

import copy
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

from .. import scanruler as sr
from .generate_api import _job, built_stl
from .paths import resolve_in_root
from .server import Download, HTTPError, route

MAX_SCAN = 500 * 1024 * 1024
MAX_SCANS = 20
CHUNK = 1 << 20
EXTS = {"stl", "ply", "obj", "3mf"}
ID_RE = re.compile(r"^[0-9a-f]{12}$")
SCANS_DIR = "scans"
_LOCK = threading.Lock()
TEMPLATE = Path(__file__).with_name("scanruler_project.json")


def _safe_name(name) -> tuple[str, str]:
    if not isinstance(name, str) or not name.strip():
        raise HTTPError(400, "name is required")
    base = os.path.basename(name.replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip(".") or "scan"
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if ext not in EXTS:
        raise HTTPError(415, "scan must be one of: " + ", ".join(sorted(EXTS)))
    if len(base) > 120:
        base = base[:-len(ext) - 1][:100] + "." + ext
    return base, ext


def _scans_root(root: Path) -> Path:
    return resolve_in_root(root, SCANS_DIR, suffixes=None)


def _entry(d: Path):
    files = [f for f in d.iterdir() if f.is_file() and not f.name.endswith(".part")] if d.is_dir() else []
    if not files:
        return None
    f = files[0]
    st = f.stat()
    return {"id": d.name, "name": f.name, "size": st.st_size, "uploaded": st.st_mtime}


def _list(root: Path) -> list[dict]:
    base = _scans_root(root)
    if not base.is_dir():
        return []
    out = [e for d in base.iterdir() if ID_RE.match(d.name) and (e := _entry(d))]
    return sorted(out, key=lambda e: e["uploaded"], reverse=True)


def _prune(root: Path) -> None:
    for e in _list(root)[MAX_SCANS:]:
        shutil.rmtree(_scans_root(root) / e["id"], ignore_errors=True)


def _scan_dir(req) -> tuple[Path, dict]:
    sid = req.params["id"]
    if not ID_RE.match(sid):
        raise HTTPError(404, "unknown scan")
    d = _scans_root(req.app.root) / sid
    e = _entry(d)
    if e is None:
        raise HTTPError(404, "unknown scan")
    return d, e


@route("PUT", "/api/scans", raw=True)
def upload(req):
    name, _ext = _safe_name(req.query.get("name"))
    if req.length > MAX_SCAN:
        left = min(req.length, MAX_SCAN)  # drain a bounded amount so the client can read the 413
        while left > 0:
            chunk = req.stream.read(min(CHUNK, left))
            if not chunk:
                break
            left -= len(chunk)
        raise HTTPError(413, f"scan too large (max {MAX_SCAN // (1024 * 1024)} MB)")
    sid = uuid.uuid4().hex[:12]
    d = _scans_root(req.app.root) / sid
    d.mkdir(parents=True)
    tmp = d / (name + ".part")
    try:
        left = req.length
        with open(tmp, "wb") as f:
            while left > 0:
                chunk = req.stream.read(min(CHUNK, left))
                if not chunk:
                    raise HTTPError(400, "upload ended early")
                f.write(chunk)
                left -= len(chunk)
        os.replace(tmp, d / name)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    with _LOCK:
        _prune(req.app.root)
    st = (d / name).stat()
    return {"id": sid, "name": name, "size": st.st_size, "uploaded": st.st_mtime}


@route("GET", "/api/scans")
def listing(req):
    return {"scans": _list(req.app.root)}


@route("DELETE", "/api/scans/{id}")
def delete(req):
    d, _e = _scan_dir(req)
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


@route("GET", "/api/scans/{id}/file/{filename}")
def scan_file(req):
    d, e = _scan_dir(req)
    if req.params["filename"] != e["name"]:
        raise HTTPError(404, "unknown scan file")
    return Download((d / e["name"]).read_bytes(), e["name"])


def build_project(scan_name: str, scan_bytes: bytes, ref_bytes: bytes, model_name: str) -> bytes:
    ext = scan_name.rsplit(".", 1)[-1].lower()
    m = copy.deepcopy(json.loads(TEMPLATE.read_text()))
    m["appVersion"] = sr.installed_version() or sr.DEFAULT_VERSION
    m["name"] = model_name
    m["workspace"] = "deviation"
    m["scan"]["fileName"] = scan_name
    m["scan"]["member"] = f"scan.{ext}"
    m["deviation"]["reference"] = {"fileName": f"{model_name}.stl", "member": "reference.stl"}
    m["deviation"]["source"] = "reference"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:  # project.json must be the first member
        z.writestr("project.json", json.dumps(m))
        z.writestr(f"scan.{ext}", scan_bytes)
        z.writestr("reference.stl", ref_bytes)
    return buf.getvalue()


@route("GET", "/api/scans/{id}/check/{jobid}/{variant}/check.scanruler")
def check(req):
    d, e = _scan_dir(req)
    req.params["id"] = req.params["jobid"]  # _job reads the job id from params["id"]
    job = _job(req)
    stl, name = built_stl(req, job, req.params["variant"])
    model = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip(".") or "model"
    data = build_project(e["name"], (d / e["name"]).read_bytes(), stl.read_bytes(), model)
    return Download(data, "check.scanruler", "application/octet-stream")


@route("GET", "/api/scanruler")
def status(req):
    return sr.info()


_UPDATER: dict = {"thread": None}


def _run_update() -> None:
    try:
        sr.update()
    except Exception as e:  # noqa: BLE001  (recorded in state.json by update())
        sys.stderr.write(f"scanruler update failed: {e}\n")


@route("POST", "/api/scanruler/update")
def start_update(req):
    with _LOCK:
        t = _UPDATER["thread"]
        if (t is not None and t.is_alive()) or sr.is_running():
            raise HTTPError(409, "an update is already running")
        t = threading.Thread(target=_run_update, daemon=True)
        _UPDATER["thread"] = t
        t.start()
    return {"started": True}
