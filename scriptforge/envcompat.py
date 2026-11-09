"""Backward compatibility for the fsgen -> ScriptForge rename: env vars and on-disk state files."""
from __future__ import annotations

import os
from pathlib import Path

PREFIX, OLD_PREFIX = "SCRIPTFORGE_", "FSGEN_"
STATE_PREFIX, OLD_STATE_PREFIX = ".scriptforge_", ".fsgen_"


def env(name: str, default: str | None = None) -> str | None:
    """Value of SCRIPTFORGE_<name>, else the legacy FSGEN_<name>, else default. `name` is given without prefix."""
    for prefix in (PREFIX, OLD_PREFIX):
        val = os.environ.get(prefix + name)
        if val is not None:
            return val
    return default


def migrate_state(path):
    """Rename a legacy `.fsgen_*` state file to its `.scriptforge_*` name once (data, e.g. the API ledger, is kept).
    Returns `path` so it can wrap an expression. Other paths are left alone."""
    if isinstance(path, Path) and path.name.startswith(STATE_PREFIX) and not path.exists():
        old = path.with_name(OLD_STATE_PREFIX + path.name[len(STATE_PREFIX):])
        if old.exists():
            os.replace(old, path)
    return path
