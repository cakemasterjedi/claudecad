"""HTTP server for `scriptforge web`: route registry, auth, JSON and SSE helpers, static files.

Route contract (for feature slices)
-----------------------------------
Add a module name to ``scriptforge.web.ROUTE_MODULES`` and in that module::

    from scriptforge.web.server import route, HTTPError

    @route("GET", "/api/scripts/{name}")          # {name} = one path segment, {rest:path} = the remainder
    def show(req):                                 # req: Request
        return {"name": req.params["name"]}        # dict -> JSON 200

Handlers receive a ``Request``: ``method``, ``path``, ``params`` (from the pattern, URL-decoded), ``query``
(first value per key), ``body`` (parsed JSON or ``None``), ``app`` (``app.root`` Path, ``app.password`` etc.)
and ``client_ip``. They return:

* a dict -> ``200 application/json``;
* ``(status, dict)`` -> that status with a JSON body;
* an iterator of ``(event, data_dict)`` -> streamed as ``text/event-stream`` (``event: <event>`` /
  ``data: <json>``), with a ``: keep-alive`` comment every 15 s while the iterator is busy;
* or raise ``HTTPError(status, message)`` -> ``{"error": message}``.

``@route(method, pattern, raw=True)`` marks a raw-body route (uploads): the body is NOT read or parsed and
the 5 MB cap does not apply; ``req.body`` is ``None``, ``req.stream`` is the socket reader and ``req.length`` the
Content-Length (400 if missing or invalid). The handler must read at most ``length`` bytes (and, to answer an
oversize upload with 413, drain what it refuses). Auth and the X-ScriptForge check still apply.

``GET /scanruler`` and ``/scanruler/...`` serve the installed ScanRuler build (see scriptforge/scanruler.py) with
its own CSP; they need the session cookie (a logged-out browser is redirected to ``/``).

Security is applied before handlers run: every ``/api/*`` route except ``POST /api/login`` needs a valid
session cookie (401), every non-GET ``/api/*`` request needs header ``X-ScriptForge: 1`` (403), bodies are capped at
1 MB (413). Always confine user-supplied file paths with ``scriptforge.web.paths.resolve_in_root``.
"""
from __future__ import annotations

import hmac
import importlib
import hashlib
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

COOKIE = "scriptforge_session"
MAX_BODY = 5_000_000
SESSION_IDLE = 30 * 24 * 3600   # a month without use; the cookie lives as long
SESSION_SAVE_EVERY = 3600
SESSION_FILE = ".scriptforge_sessions.json"
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 600
LOGIN_LOCKOUT = 600
SSE_KEEPALIVE = 15.0
STATIC_DIR = Path(__file__).parent / "static"
OPTIONAL_STATIC = {"parts.js", "generate.js"}  # tab modules of later slices; served empty when absent

CSP = ("default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
       "style-src 'self'; connect-src 'self'; img-src 'self' data: blob:; object-src 'none'; "
       "base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

# ScanRuler (served under /scanruler/) keeps its own policy; only framing allows ScriptForge's same-origin iframe.
SCANRULER_CSP = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; "
                 "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
                 "connect-src 'self' https: data: blob: http://127.0.0.1:* ws://127.0.0.1:* http://localhost:* "
                 "ws://localhost:*; media-src 'self' blob:; object-src 'none'; base-uri 'self'; "
                 "form-action 'self'; frame-ancestors 'self'")
SCANRULER_WORKER_CSP = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval' 'unsafe-eval'; "
                        "connect-src 'self' data: blob:; object-src 'none'; base-uri 'self'")
SCANRULER_WORKER_RE = re.compile(r"^(meshWorker|reWorker|reHelperWorker)-")
SCANRULER_SECURITY = {
    "Content-Security-Policy": SCANRULER_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "SAMEORIGIN",
}
SCANRULER_MIME = {".wasm": "application/wasm", ".js": "text/javascript; charset=utf-8",
                  ".mjs": "text/javascript; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml",
                  ".json": "application/json"}


class HTTPError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message or HTTPStatus(status).phrase


@dataclass
class Request:
    method: str
    path: str
    params: dict
    query: dict
    body: object
    app: "App"
    client_ip: str = ""
    headers: object = None
    stream: object = None   # raw routes only: the request body reader
    length: int = 0         # raw routes only: Content-Length


# --- route registry ---------------------------------------------------------------------------------------

ROUTES: list[tuple[str, re.Pattern, object]] = []
RAW_ROUTES: set = set()


def _compile(pattern: str) -> re.Pattern:
    def sub(m):
        name, kind = m.group(1), m.group(2)
        return f"(?P<{name}>{'.+' if kind == 'path' else '[^/]+'})"
    return re.compile("^" + re.sub(r"\{(\w+)(?::(path))?\}", sub, pattern) + "$")


class Download:
    """Handler return value: a file sent as an attachment (filename is reduced to [A-Za-z0-9._-])."""

    def __init__(self, body: bytes, filename: str, ctype: str = "application/octet-stream"):
        self.body, self.filename, self.ctype = body, filename, ctype


def route(method: str, pattern: str, raw: bool = False):
    """Decorator: register `fn(req) -> dict | (status, dict) | iterator of (event, data)` for METHOD pattern.
    raw=True: the body is left unread; see Request.stream / Request.length."""
    def deco(fn):
        if raw:
            RAW_ROUTES.add(fn)
        ROUTES.append((method.upper(), _compile(pattern), fn))
        return fn
    return deco


def register(module) -> None:
    """Import a route module (name or module object); its @route decorators populate ROUTES."""
    if isinstance(module, str):
        importlib.import_module(module)


def load_routes() -> None:
    from . import ROUTE_MODULES
    for m in ROUTE_MODULES:
        register(m)


def find_route(method: str, path: str):
    """-> (fn, params) or raises 404/405."""
    allowed = False
    for m, rx, fn in ROUTES:
        hit = rx.match(path)
        if hit:
            if m == method:
                return fn, {k: unquote(v) for k, v in hit.groupdict().items()}
            allowed = True
    raise HTTPError(405 if allowed else 404, "method not allowed" if allowed else "not found")


# --- sessions and rate limiting ---------------------------------------------------------------------------

class SessionStore:
    """Login tokens with an idle timeout. With `path`, sessions survive a server restart: the file (mode 600) holds
    only SHA-256 hashes of the tokens and their last-use times, never the tokens themselves."""

    def __init__(self, idle=SESSION_IDLE, clock=time.time, path: Path | None = None):
        self.idle, self.clock, self.path = idle, clock, path
        self._tokens: dict[str, float] = {}   # sha256(token) -> last use
        self._saved_at = 0.0
        self._lock = threading.Lock()
        self._load()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text())
            now = self.clock()
            self._tokens = {k: float(v) for k, v in data.items()
                            if isinstance(k, str) and len(k) == 64 and now - float(v) <= self.idle}
        except (OSError, ValueError, TypeError, AttributeError):
            self._tokens = {}

    def _save(self) -> None:  # caller holds the lock
        if not self.path:
            return
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self._tokens, f)
            os.replace(tmp, self.path)
            self._saved_at = self.clock()
        except OSError as e:
            sys.stderr.write(f"warning: could not save sessions: {e}\n")

    def create(self) -> str:
        token = secrets.token_urlsafe(32)  # 32 random bytes
        with self._lock:
            self._tokens[self._key(token)] = self.clock()
            self._save()
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        key = self._key(token)
        with self._lock:
            now = self.clock()
            for t in [t for t, seen in self._tokens.items() if now - seen > self.idle]:
                del self._tokens[t]
            if key not in self._tokens:
                return False
            self._tokens[key] = now  # idle timeout: each use refreshes
            if now - self._saved_at > SESSION_SAVE_EVERY:  # refreshes reach the file at most once an hour
                self._save()
            return True

    def keep_only(self, token: str) -> None:
        key = self._key(token)
        with self._lock:
            self._tokens = {t: seen for t, seen in self._tokens.items() if t == key}
            self._save()

    def drop(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._tokens.pop(self._key(token), None)
            self._save()


class LoginLimiter:
    """After `max_fails` failures within `window` s from one IP, refuse that IP for `lockout` s."""

    def __init__(self, max_fails=LOGIN_MAX_FAILS, window=LOGIN_WINDOW, lockout=LOGIN_LOCKOUT, clock=time.monotonic):
        self.max_fails, self.window, self.lockout, self.clock = max_fails, window, lockout, clock
        self._fails: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            until = self._blocked.get(ip)
            if until is None:
                return False
            if self.clock() >= until:
                del self._blocked[ip]
                return False
            return True

    def failure(self, ip: str) -> None:
        with self._lock:
            now = self.clock()
            fails = [t for t in self._fails.get(ip, []) if now - t < self.window] + [now]
            if len(fails) >= self.max_fails:
                self._blocked[ip] = now + self.lockout
                fails = []
            self._fails[ip] = fails

    def success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


@dataclass
class App:
    root: Path
    password: str
    sessions: SessionStore = field(default_factory=SessionStore)
    limiter: LoginLimiter = field(default_factory=LoginLimiter)
    sse_keepalive: float = SSE_KEEPALIVE


# --- request handling -------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "scriptforge-web"
    sys_version = ""
    app: App

    def log_message(self, fmt, *args):  # method + path only (no query strings, never bodies or cookies)
        sys.stderr.write(f"{self.address_string()} {self.command} {urlsplit(self.path).path} -> "
                         f"{args[1] if len(args) > 1 else ''}\n")

    # -- output helpers
    def _headers(self, status, ctype, length=None, extra=(), security=None, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k, v in (SECURITY_HEADERS if security is None else security).items():
            self.send_header(k, v)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Cache-Control", cache)
        self.end_headers()

    def _send(self, status, body: bytes, ctype, extra=(), security=None, cache="no-store"):
        self._headers(status, ctype, len(body), extra, security, cache)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj, extra=()):
        self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8", extra)

    # -- dispatch
    def do_GET(self):
        self._dispatch()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_GET

    def _dispatch(self):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            if path.startswith("/api/") or path == "/api":
                self._api(path, dict(parse_qsl(parts.query)))
            elif self.command in ("GET", "HEAD") and (path == "/scanruler" or path.startswith("/scanruler/")):
                self._scanruler(path)
            elif self.command in ("GET", "HEAD") and path == "/scanruler-mark.svg":
                # ScanRuler's TopBar uses this root-absolute URL; same build, auth and policy as /scanruler/.
                self._scanruler("/scanruler/scanruler-mark.svg")
            elif self.command == "GET" and (path == "/" or path.startswith("/static/")):
                self._static(path)
            else:
                raise HTTPError(404, "not found")
        except HTTPError as e:
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"internal error: {type(e).__name__}: {e}\n")
            try:
                self._json(500, {"error": "internal error"})
            except OSError:
                pass

    def _cookie(self) -> str | None:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except Exception:  # noqa: BLE001
            return None
        m = jar.get(COOKIE)
        return m.value if m else None

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HTTPError(400, "bad Content-Length")
        if n < 0:
            raise HTTPError(400, "bad Content-Length")
        if n > MAX_BODY:
            left = min(n, 8 * MAX_BODY)  # drain so the client can read our answer
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
            raise HTTPError(413, f"request body too large (max {MAX_BODY // 1_000_000} MB)")
        if n == 0:
            return None
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except ValueError:
            raise HTTPError(400, "invalid JSON body")

    def _api(self, path, query):
        app = self.server.app
        ip = self._client_ip()
        if self.command != "GET" and self.headers.get("X-ScriptForge") != "1":
            raise HTTPError(403, "missing X-ScriptForge header")
        if path == "/api/login" and self.command == "POST":
            return self._login(app, ip)
        token = self._cookie()
        if path == "/api/logout" and self.command == "POST":
            app.sessions.drop(token)
            return self._json(200, {"ok": True}, [("Set-Cookie", f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])
        if not app.sessions.valid(token):
            raise HTTPError(401, "login required")
        fn, params = find_route(self.command, path)
        if fn in RAW_ROUTES:
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                raise HTTPError(400, "Content-Length required")
            if length < 0:
                raise HTTPError(400, "bad Content-Length")
            req = Request(self.command, path, params, query, None, app, ip, self.headers, self.rfile, length)
        else:
            req = Request(self.command, path, params, query, self._read_body(), app, ip, self.headers)
        result = fn(req)
        if isinstance(result, Download):
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", result.filename).strip(".") or "download"
            self._send(200, result.body, result.ctype, [("Content-Disposition", f'attachment; filename="{name}"')])
        elif isinstance(result, dict):
            self._json(200, result)
        elif isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            self._json(result[0], result[1])
        else:
            self._sse(result, app.sse_keepalive)

    def _client_ip(self) -> str:
        """The peer address; behind a local Cloudflare Tunnel (peer is loopback) the visitor's CF-Connecting-IP, so the
        login limiter throttles each visitor instead of everyone at once. Remote peers cannot set it for themselves."""
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1"):
            cf = (self.headers.get("CF-Connecting-IP") or "").strip()
            if cf and len(cf) <= 45 and re.fullmatch(r"[0-9A-Fa-f:.]+", cf):
                return cf
        return peer

    def _login(self, app, ip):
        if app.limiter.blocked(ip):
            raise HTTPError(429, "too many failed logins; try again later")
        body = self._read_body()
        pw = body.get("password") if isinstance(body, dict) else None
        ok = isinstance(pw, str) and hmac.compare_digest(pw.encode(), app.password.encode())
        if not ok:
            app.limiter.failure(ip)
            raise HTTPError(401, "wrong password")
        app.limiter.success(ip)
        token = app.sessions.create()
        self._json(200, {"ok": True}, [("Set-Cookie", f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_IDLE}")])

    def _sse(self, events, keepalive):
        self._headers(200, "text/event-stream; charset=utf-8", extra=[("Connection", "close")])
        q: queue.Queue = queue.Queue()
        stop = threading.Event()
        done = object()

        def pump():
            try:
                for item in events:
                    if stop.is_set():
                        break
                    q.put(item)
            except Exception as e:  # noqa: BLE001
                q.put(("error", {"message": str(e)}))
            finally:
                close = getattr(events, "close", None)
                if close:
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
                q.put(done)

        threading.Thread(target=pump, daemon=True).start()
        try:
            while True:
                try:
                    item = q.get(timeout=keepalive)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if item is done:
                    break
                event, data = item
                self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
        finally:
            stop.set()
        self.close_connection = True

    def _scanruler(self, path):
        from .. import scanruler as sr
        if path == "/scanruler":
            return self._send(302, b"", "text/plain", [("Location", "/scanruler/")], SCANRULER_SECURITY, "no-cache")
        if not self.server.app.sessions.valid(self._cookie()):
            return self._send(302, b"", "text/plain", [("Location", "/")], SCANRULER_SECURITY, "no-store")
        build = sr.current_build()
        if build is None:
            return self._send(503, b"ScanRuler is not installed. Run: scriptforge scanruler update",
                              "text/plain; charset=utf-8", (), SCANRULER_SECURITY, "no-store")
        rel = unquote(path[len("/scanruler/"):])
        if "\0" in rel:
            raise HTTPError(400, "invalid path")
        p = (build / rel).resolve()
        if not p.is_relative_to(build):
            raise HTTPError(403, "forbidden")
        in_assets = rel.split("/", 1)[0] == "assets"
        if not p.is_file():
            if in_assets:
                raise HTTPError(404, "not found")
            p, rel, in_assets = build / "index.html", "index.html", False
            if not p.is_file():
                raise HTTPError(404, "not found")
        ctype = SCANRULER_MIME.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        sec = dict(SCANRULER_SECURITY)
        if in_assets and SCANRULER_WORKER_RE.match(p.name):
            sec["Content-Security-Policy"] = SCANRULER_WORKER_CSP
        cache = "public, max-age=31536000, immutable" if in_assets else "no-cache"
        with open(p, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            self._headers(200, ctype, size, (), sec, cache)
            if self.command != "HEAD":
                shutil.copyfileobj(f, self.wfile, 1 << 20)

    def _static(self, path):
        rel = "index.html" if path == "/" else unquote(path[len("/static/"):])
        base = STATIC_DIR.resolve()
        if "\0" in rel:
            raise HTTPError(400, "invalid path")
        p = (base / rel).resolve()
        if not p.is_relative_to(base):
            raise HTTPError(403, "forbidden")
        if not p.is_file():
            if rel in OPTIONAL_STATIC:
                return self._send(200, b"", "text/javascript; charset=utf-8")
            raise HTTPError(404, "not found")
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype in ("text/javascript", "application/javascript"):
            ctype = "text/javascript"
        if ctype.startswith("text/") or ctype == "text/javascript":
            ctype += "; charset=utf-8"
        self._send(200, p.read_bytes(), ctype)


def make_server(root, host="127.0.0.1", port=8770, password="") -> tuple[ThreadingHTTPServer, App]:
    """Create (not start) the server. Use port 0 for an ephemeral port: httpd.server_address[1]."""
    if not password:
        raise ValueError("scriptforge web needs a password")
    load_routes()
    root = Path(root).resolve()
    app = App(root, password, sessions=SessionStore(path=root / SESSION_FILE))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.app = app
    return httpd, app
