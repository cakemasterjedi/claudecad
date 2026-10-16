"""Photo uploads and measurement extraction for the Generate tab.

POST /api/uploads   {filename, data: base64 (data: URL prefix allowed)} -> {"id", "type"}  (400/413/415)
GET  /api/llm       -> {"ollama": {available, model, models}, "measure_default": "ollama"|"claude"}
POST /api/measure   {images: [ids], note?, backend?: "ollama"|"claude"} -> {"text", "backend", "seconds"}
                    (400 bad input, 409 busy, 502 LLM failure)
Uploads live in <root>/out/web/uploads/<id>.<ext>; only the newest MAX_UPLOADS are kept.
"""
from __future__ import annotations

import base64
import binascii
import re
import threading
import time
import uuid
from pathlib import Path

from ..llm import LLMError, OllamaLLM, claude_llm, ollama_status
from .paths import resolve_in_root
from .server import HTTPError, route

MAX_UPLOAD = 3_000_000
MAX_UPLOADS = 50
MAX_IMAGES = 4
MAX_NOTE = 2000
ID_RE = re.compile(r"^[0-9a-f]{12}$")
UPLOAD_DIR = "out/web/uploads"
_MEASURE_LOCK = threading.Lock()


def sniff(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def upload_paths(root: Path, ids) -> list[Path]:
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPError(400, "images must be a list of upload ids")
    if len(ids) > MAX_IMAGES:
        raise HTTPError(400, f"at most {MAX_IMAGES} images")
    out = []
    for i in ids:
        if not ID_RE.match(i):
            raise HTTPError(400, f"invalid image id: {i[:20]}")
        found = None
        for ext in ("jpeg", "png", "webp"):
            p = resolve_in_root(root, f"{UPLOAD_DIR}/{i}.{ext}", suffixes=None)
            if p.is_file():
                found = p
                break
        if found is None:
            raise HTTPError(400, f"unknown image id: {i}")
        out.append(found)
    return out


def _prune(d: Path) -> None:
    files = sorted((p for p in d.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
    for p in files[:-MAX_UPLOADS]:
        p.unlink(missing_ok=True)


@route("POST", "/api/uploads")
def upload(req):
    b = req.body
    if not isinstance(b, dict) or not isinstance(b.get("data"), str):
        raise HTTPError(400, "JSON body with base64 'data' required")
    raw = b["data"]
    if raw.startswith("data:"):
        raw = raw.partition(",")[2]
    if len(raw) > MAX_UPLOAD * 4 // 3 + 16:
        raise HTTPError(413, f"image too large (max {MAX_UPLOAD // 1_000_000} MB)")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPError(400, "invalid base64")
    if len(data) > MAX_UPLOAD:
        raise HTTPError(413, f"image too large (max {MAX_UPLOAD // 1_000_000} MB)")
    kind = sniff(data)
    if kind is None:
        raise HTTPError(415, "only jpeg, png and webp images are accepted")
    uid = uuid.uuid4().hex[:12]
    path = resolve_in_root(req.app.root, f"{UPLOAD_DIR}/{uid}.{kind}", suffixes=None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _prune(path.parent)
    return {"id": uid, "type": kind}


@route("GET", "/api/llm")
def llm_info(req):
    st = ollama_status()
    return {"ollama": st, "measure_default": "ollama" if st["available"] else "claude"}


@route("POST", "/api/measure")
def measure(req):
    from ..native import prompts
    b = req.body
    if not isinstance(b, dict):
        raise HTTPError(400, "JSON body required")
    ids = b.get("images")
    if not isinstance(ids, list) or not ids:
        raise HTTPError(400, "at least one image is required")
    paths = upload_paths(req.app.root, ids)
    note = b.get("note", "")
    if note is None:
        note = ""
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise HTTPError(400, f"note must be text of at most {MAX_NOTE} characters")
    backend = b.get("backend")
    if backend is None:
        backend = "ollama" if ollama_status()["available"] else "claude"
    if backend not in ("ollama", "claude"):
        raise HTTPError(400, "backend must be 'ollama' or 'claude'")
    if not _MEASURE_LOCK.acquire(blocking=False):
        raise HTTPError(409, "a measurement is already running")
    try:
        t0 = time.time()
        try:
            llm = OllamaLLM() if backend == "ollama" else claude_llm(web=False)
            system, prompt = prompts.measure_prompt(note)
            text = llm.complete(system, prompt, images=paths)
        except LLMError as e:
            raise HTTPError(502, str(e))
        return {"text": text.strip(), "backend": backend, "seconds": round(time.time() - t0, 1)}
    finally:
        _MEASURE_LOCK.release()
