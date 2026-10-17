"""HTTP server for `fsgen web`: route registry, auth, JSON and SSE helpers, static files.

Route contract (for feature slices)
-----------------------------------
Add a module name to ``fsgen.web.ROUTE_MODULES`` and in that module::

    from fsgen.web.server import route, HTTPError

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

Security is applied before handlers run: every ``/api/*`` route except ``POST /api/login`` needs a valid
session cookie (401), every non-GET ``/api/*`` request needs header ``X-Fsgen: 1`` (403), bodies are capped at
1 MB (413). Always confine user-supplied file paths with ``fsgen.web.paths.resolve_in_root``.
"""
from __future__ import annotations

import hmac
import importlib
import json
import mimetypes
import queue
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

COOKIE = "fsgen_session"
MAX_BODY = 5_000_000
SESSION_IDLE = 12 * 3600
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


# --- route registry ---------------------------------------------------------------------------------------

ROUTES: list[tuple[str, re.Pattern, object]] = []


def _compile(pattern: str) -> re.Pattern:
    def sub(m):
        name, kind = m.group(1), m.group(2)
        return f"(?P<{name}>{'.+' if kind == 'path' else '[^/]+'})"
    return re.compile("^" + re.sub(r"\{(\w+)(?::(path))?\}", sub, pattern) + "$")


class Download:
    """Handler return value: a file sent as an attachment (filename is reduced to [A-Za-z0-9._-])."""

    def __init__(self, body: bytes, filename: str, ctype: str = "application/octet-stream"):
        self.body, self.filename, self.ctype = body, filename, ctype


def route(method: str, pattern: str):
    """Decorator: register `fn(req) -> dict | (status, dict) | iterator of (event, data)` for METHOD pattern."""
    def deco(fn):
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
    def __init__(self, idle=SESSION_IDLE, clock=time.monotonic):
        self.idle, self.clock = idle, clock
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)  # 32 random bytes
        with self._lock:
            self._tokens[token] = self.clock()
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            now = self.clock()
            for t in [t for t, seen in self._tokens.items() if now - seen > self.idle]:
                del self._tokens[t]
            if token not in self._tokens:
                return False
            self._tokens[token] = now  # idle timeout: each use refreshes
            return True

    def keep_only(self, token: str) -> None:
        with self._lock:
            self._tokens = {t: seen for t, seen in self._tokens.items() if t == token}

    def drop(self, token: str | None) -> None:
        with self._lock:
            self._tokens.pop(token, None)


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
    server_version = "fsgen-web"
    sys_version = ""
    app: App

    def log_message(self, fmt, *args):  # method + path only (no query strings, never bodies or cookies)
        sys.stderr.write(f"{self.address_string()} {self.command} {urlsplit(self.path).path} -> "
                         f"{args[1] if len(args) > 1 else ''}\n")

    # -- output helpers
    def _headers(self, status, ctype, length=None, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send(self, status, body: bytes, ctype, extra=()):
        self._headers(status, ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj, extra=()):
        self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8", extra)

    # -- dispatch
    def do_GET(self):
        self._dispatch()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_GET

    def _dispatch(self):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            if path.startswith("/api/") or path == "/api":
                self._api(path, dict(parse_qsl(parts.query)))
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
        ip = self.client_address[0]
        if self.command != "GET" and self.headers.get("X-Fsgen") != "1":
            raise HTTPError(403, "missing X-Fsgen header")
        if path == "/api/login" and self.command == "POST":
            return self._login(app, ip)
        token = self._cookie()
        if path == "/api/logout" and self.command == "POST":
            app.sessions.drop(token)
            return self._json(200, {"ok": True}, [("Set-Cookie", f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])
        if not app.sessions.valid(token):
            raise HTTPError(401, "login required")
        fn, params = find_route(self.command, path)
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
        self._json(200, {"ok": True}, [("Set-Cookie", f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/")])

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
        raise ValueError("fsgen web needs a password")
    load_routes()
    app = App(Path(root).resolve(), password)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.app = app
    return httpd, app
