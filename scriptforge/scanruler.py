"""ScanRuler hosting: install paths, state and the release updater (`scriptforge scanruler {status,update}`).

Layout under the base dir (env SCANRULER_HOME, default ~/.local/share/scriptforge/scanruler):
  <base>/<tag>/            git checkout of a release tag (vX.Y.Z) with a built dist-sf/
  <base>/current           symlink -> <tag>/dist-sf (the active build; resolved on every request)
  <base>/state.json        {version, latest, checked_at, error}
  <base>/update.lock, update.log
ScanRuler is AGPL and lives upstream; it is only built and served here, never copied into this repo.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = "https://github.com/CNCKitchen/ScanRuler.git"
DEFAULT_VERSION = "0.11.1"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
MIN_NODE = 22
TIMEOUTS = {"clone": 300, "npm": 900, "build": 900}


class UpdateError(Exception):
    pass


class AlreadyRunning(UpdateError):
    pass


def base_dir() -> Path:
    env = os.environ.get("SCANRULER_HOME")
    return Path(env).expanduser() if env else Path.home() / ".local/share/scriptforge/scanruler"


def current_link(base: Path | None = None) -> Path:
    return (base or base_dir()) / "current"


def current_build(base: Path | None = None) -> Path | None:
    """The directory `current` points to, if it exists (resolved fresh on every call)."""
    link = current_link(base)
    try:
        p = link.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return p if p.is_dir() else None


def installed_version(base: Path | None = None) -> str | None:
    p = current_build(base)
    if p is None:
        return None
    for part in (p.parent.name, p.name):
        m = TAG_RE.match(part)
        if m:
            return part[1:]
    return None


def read_state(base: Path | None = None) -> dict:
    try:
        d = json.loads(((base or base_dir()) / "state.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(base: Path, **updates) -> dict:
    base.mkdir(parents=True, exist_ok=True)
    st = read_state(base)
    st.update(updates)
    tmp = base / "state.json.tmp"
    tmp.write_text(json.dumps(st))
    os.replace(tmp, base / "state.json")
    return st


def is_running(base: Path | None = None) -> bool:
    base = base or base_dir()
    lock = base / "update.lock"
    if not lock.exists():
        return False
    with open(lock, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def info(base: Path | None = None) -> dict:
    base = base or base_dir()
    st = read_state(base)
    ver = installed_version(base)
    return {"installed": current_build(base) is not None, "version": ver, "latest": st.get("latest"),
            "checked_at": st.get("checked_at"), "updating": is_running(base), "error": st.get("error")}


def tag_key(tag: str):
    m = TAG_RE.match(tag)
    return tuple(int(x) for x in m.groups()) if m else None


def pick_latest(ls_remote_output: str) -> str | None:
    best = None
    for line in ls_remote_output.splitlines():
        ref = line.split("\t")[-1].strip()
        tag = ref[len("refs/tags/"):] if ref.startswith("refs/tags/") else ""
        k = tag_key(tag)
        if k and (best is None or k > best[0]):
            best = (k, tag)
    return best[1] if best else None


def verify_build(dist: Path) -> None:
    index = dist / "index.html"
    if not index.is_file():
        raise UpdateError(f"{dist}: index.html is missing")
    html = index.read_text(errors="replace")
    root = dist.resolve()
    for ref in re.findall(r'(?:src|href)="/scanruler/([^"#?]*)', html):
        if not ref:
            continue
        p = (root / ref).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise UpdateError(f"index.html references /scanruler/{ref}, which is not in the build")


def swap_current(base: Path, tag: str) -> None:
    tmp = base / ".current.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(f"{tag}/dist-sf", tmp)
    os.replace(tmp, base / "current")


def prune(base: Path, keep: int = 2) -> None:
    cur = current_build(base)
    cur_dir = cur.parent if cur is not None else None
    tags = sorted((p for p in base.iterdir() if p.is_dir() and not p.is_symlink() and tag_key(p.name)),
                  key=lambda p: tag_key(p.name), reverse=True)
    keepers = set(tags[:keep])
    for p in tags:
        if p in keepers or (cur_dir is not None and p.resolve() == cur_dir.resolve()):
            continue
        shutil.rmtree(p, ignore_errors=True)


def default_runner(cmd, cwd, timeout, log):
    """Run cmd, append its output to `log`; -> (returncode, stdout)."""
    with open(log, "ab") as f:
        f.write(f"\n$ {' '.join(cmd)}  (cwd {cwd})\n".encode())
        f.flush()
        try:
            r = subprocess.run(cmd, cwd=cwd, timeout=timeout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except subprocess.TimeoutExpired:
            f.write(b"timed out\n")
            return 124, ""
        f.write(r.stdout)
    return r.returncode, r.stdout.decode(errors="replace")


def _node_major(runner, base, log) -> int:
    rc, out = runner(["node", "--version"], base, 30, log)
    m = re.match(r"v?(\d+)", out.strip())
    if rc != 0 or not m:
        raise UpdateError("node is not available (Node 22 or newer is required)")
    return int(m.group(1))


def update(base: Path | None = None, runner=default_runner, repo: str = REPO) -> str:
    """Install the newest release tag. -> "up-to-date" | "adopted <tag>" | "installed <tag>".
    Raises AlreadyRunning or UpdateError (the error is also recorded in state.json)."""
    base = base or base_dir()
    base.mkdir(parents=True, exist_ok=True)
    lockf = open(base / "update.lock", "a+")
    try:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AlreadyRunning("an update is already running")
        log = base / "update.log"
        staging = None
        try:
            rc, out = runner(["git", "ls-remote", "--tags", "--refs", repo], base, TIMEOUTS["clone"], log)
            tag = pick_latest(out) if rc == 0 else None
            if tag is None:
                raise UpdateError("could not list ScanRuler release tags" if rc else "no release tags found")
            write_state(base, latest=tag[1:], checked_at=time.time())
            dist = base / tag / "dist-sf"
            cur = current_build(base)
            if dist.is_dir() and cur is not None and cur.resolve() == dist.resolve():
                verify_build(dist)
                write_state(base, version=tag[1:], error=None)
                return "up-to-date"
            if dist.is_dir():  # built earlier (or by hand): verify and link
                verify_build(dist)
                swap_current(base, tag)
                write_state(base, version=tag[1:], error=None)
                prune(base)
                return f"adopted {tag}"
            if _node_major(runner, base, log) < MIN_NODE:
                raise UpdateError(f"Node {MIN_NODE} or newer is required")
            staging = base / f".staging-{tag}"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            steps = [(["git", "clone", "--depth", "1", "--branch", tag, repo, "."], "clone"),
                     (["npm", "ci"], "npm"),
                     (["npx", "vite", "build", "--base=/scanruler/", "--outDir", "dist-sf", "--emptyOutDir"], "build")]
            for cmd, kind in steps:
                rc, _ = runner(cmd, staging, TIMEOUTS[kind], log)
                if rc != 0:
                    raise UpdateError(f"{' '.join(cmd[:2])} failed (exit {rc}); see update.log")
            verify_build(staging / "dist-sf")
            final = base / tag
            if final.exists():
                shutil.rmtree(final)  # an unusable earlier attempt (no passing dist-sf)
            os.rename(staging, final)
            staging = None
            swap_current(base, tag)
            write_state(base, version=tag[1:], error=None)
            prune(base)
            return f"installed {tag}"
        except UpdateError as e:
            write_state(base, error=str(e))
            raise
        except Exception as e:  # noqa: BLE001
            write_state(base, error=f"{type(e).__name__}: {e}")
            raise UpdateError(str(e)) from e
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
    finally:
        lockf.close()  # releases the flock


def status_text(base: Path | None = None) -> str:
    i = info(base)
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(i["checked_at"])) if i["checked_at"] else "never"
    return (f"installed: {i['version'] or ('unknown' if i['installed'] else 'no')}\n"
            f"latest:    {i['latest'] or 'unknown'}\nchecked:   {when}\nerror:     {i['error'] or '-'}")


def cmd_scanruler(args) -> int:
    if args.action == "status":
        print(status_text())
        return 0
    try:
        print(update())
        return 0
    except AlreadyRunning as e:
        print(str(e), file=sys.stderr)
        return 2
    except UpdateError as e:
        print(f"update failed: {e}", file=sys.stderr)
        return 1
