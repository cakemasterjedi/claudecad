"""Path confinement for the web UI: every user-supplied path goes through resolve_in_root()."""
from __future__ import annotations

import os
from pathlib import Path

from .server import HTTPError

MAX_SCRIPTS = 2000
SKIP_DIRS = {"out", "node_modules", "__pycache__"}


def resolve_in_root(root: Path, rel: str, suffixes=(".fs",)) -> Path:
    """Resolve `rel` under `root`; raise HTTPError for anything that could escape or has the wrong suffix."""
    if not isinstance(rel, str) or not rel:
        raise HTTPError(400, "path required")
    if "\0" in rel:
        raise HTTPError(400, "invalid path")
    if rel.startswith(("/", "\\")) or Path(rel).is_absolute() or (len(rel) > 1 and rel[1] == ":"):
        raise HTTPError(400, "absolute paths are not allowed")
    if ".." in rel.replace("\\", "/").split("/"):
        raise HTTPError(400, "'..' is not allowed")
    root_r = root.resolve()
    p = (root_r / rel).resolve()
    if not p.is_relative_to(root_r):
        raise HTTPError(403, "path outside the project root")
    if suffixes is not None and p.suffix.lower() not in tuple(s.lower() for s in suffixes):
        raise HTTPError(400, f"only {', '.join(suffixes)} files are allowed")
    return p


def list_scripts(root: Path) -> list[str]:
    """Sorted relative POSIX paths of *.fs under root (capped), skipping hidden/build/venv dirs and escapes."""
    root_r = root.resolve()
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_r, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for name in filenames:
            if not name.endswith(".fs"):
                continue
            p = Path(dirpath) / name
            try:
                if not p.resolve().is_relative_to(root_r):
                    continue
            except (OSError, RuntimeError):
                continue
            found.append(p.relative_to(root_r).as_posix())
    return sorted(found)[:MAX_SCRIPTS]
