"""Copy finished outputs to Dropbox (SCRIPTFORGE_DROPBOX_DIR) and open STEP files in FreeCAD."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from dotenv import load_dotenv

from .envcompat import env


def to_dropbox(name: str, files) -> list[Path]:
    load_dotenv()
    root = env("DROPBOX_DIR")
    if not root:
        return []
    dest = Path(root) / name.replace("/", "_")
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        f = Path(f)
        if f.exists():
            shutil.copy2(f, dest / f.name)
            out.append(dest / f.name)
    return out


def open_in_freecad(path: Path) -> bool:
    app = Path("/Applications/FreeCAD.app")
    if not app.exists() or not Path(path).exists():
        return False
    subprocess.Popen(["open", "-a", str(app), str(path)])
    return True
