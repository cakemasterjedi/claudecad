"""POST /api/password: change the web password (needs the current one); saved to .env so it survives restarts."""
from __future__ import annotations

import hmac
import re
from http.cookies import SimpleCookie
from pathlib import Path

from .server import COOKIE, HTTPError, route

MIN_LEN = 8


def save_password(env_path: Path, password: str) -> None:
    """Validate and store FSGEN_WEB_PASSWORD in .env (mode 600). Raises ValueError when unacceptable."""
    if len(password) < MIN_LEN or "'" in password or "\n" in password or "\r" in password:
        raise ValueError(f"password must be at least {MIN_LEN} characters and contain no ' or newline")
    line = f"FSGEN_WEB_PASSWORD='{password}'"
    text = env_path.read_text() if env_path.exists() else ""
    if re.search(r"^FSGEN_WEB_PASSWORD=.*$", text, re.M):
        text = re.sub(r"^FSGEN_WEB_PASSWORD=.*$", lambda _: line, text, flags=re.M)
    else:
        text += ("\n" if text and not text.endswith("\n") else "") + line + "\n"
    env_path.write_text(text)
    env_path.chmod(0o600)


@route("POST", "/api/password")
def change_password(req):
    body = req.body if isinstance(req.body, dict) else {}
    cur, new = body.get("current"), body.get("new")
    if not isinstance(cur, str) or not isinstance(new, str):
        raise HTTPError(400, "current and new password are required")
    app = req.app
    if app.limiter.blocked(req.client_ip):
        raise HTTPError(429, "too many failed attempts; try again later")
    if not hmac.compare_digest(cur.encode(), app.password.encode()):
        app.limiter.failure(req.client_ip)
        raise HTTPError(403, "current password is wrong")
    try:
        save_password(app.root / ".env", new)
    except ValueError as e:
        raise HTTPError(400, str(e))
    app.password = new
    jar = SimpleCookie()
    jar.load(req.headers.get("Cookie", ""))
    app.sessions.keep_only(jar[COOKIE].value)  # other logged-in browsers must log in again
    return {"ok": True}
