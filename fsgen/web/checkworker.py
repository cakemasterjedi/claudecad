"""Run `fsgen studio check` for the web UI in a separate worker process.

A script that hangs or crashes the geometry kernel must not take the server down, so the work runs in a
long-lived spawned process (created lazily, one check at a time). On timeout the worker is killed and recreated.
`run_check` is the function executed in the worker; `check_in_worker` is what request handlers call.
"""
from __future__ import annotations

import atexit
import contextlib
import io
import multiprocessing
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

DEFAULT_TIMEOUT = 60.0
MAX_WALLS = 20


def check_timeout() -> float:
    try:
        return float(os.environ.get("FSGEN_WEB_CHECK_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError:
        return DEFAULT_TIMEOUT


# --- executed in the worker process -----------------------------------------------------------------------

def run_check(path_str: str, min_wall: float = 1.2) -> dict:
    """Same work as `fsgen studio check` plus viewer scenes; 0 Onshape calls, writes no files."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return _run_check(Path(path_str), min_wall)


def _run_check(path: Path, min_wall: float) -> dict:
    from ..native.script import trace

    t0 = time.monotonic()
    res: dict = {"ok": False, "stage": "script", "error": None, "features": [], "report": "", "print_check": "",
                 "validity": [], "validity_text": "", "walls": [], "walls_text": "", "scenes": [], "seconds": 0.0}
    try:
        tr = trace(path.read_text())
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    res["features"] = [{"type": f.feature_type, "name": f.name} for f in tr.features]
    if not tr.ok:
        res["error"] = tr.error or "script error"
        if tr.error_line and "line" not in res["error"]:
            res["error"] += f" (script line {tr.error_line})"
        res["seconds"] = round(time.monotonic() - t0, 2)
        return res
    try:
        from ..usage import estimate_push
        res["push_estimate"] = estimate_push(tr, None).text()
    except Exception:  # noqa: BLE001
        pass

    from ..native import geomcheck
    from ..native.local import build_local
    res["stage"] = "local"
    loc = build_local(tr)
    res["report"] = loc.report() if loc.ok else ""
    if not loc.ok:
        res["error"] = loc.error or "local build failed"
        res["seconds"] = round(time.monotonic() - t0, 2)
        return res
    solids = [b.shape for b in loc.run.solids]
    valid = True
    if solids:
        from ..native.printcheck import overhangs, report as print_report
        res["print_check"] = print_report(overhangs(solids))
        checked = geomcheck.validity(solids)
        valid = all(v.ok for v in checked)
        res["validity"] = [{"part": v.part, "ok": v.ok, "problems": list(v.problems)} for v in checked]
        res["validity_text"] = geomcheck.validity_report(checked)
        walls = geomcheck.thin_walls(solids, min_wall)
        res["walls_text"] = geomcheck.thin_wall_report(walls, min_wall)
        res["walls"] = [{"part": w.part, "min_mm": round(w.min_mm, 3), "area_mm2": round(w.area_mm2, 1),
                         "center_mm": [round(c, 2) for c in w.center_mm]} for w in walls[:MAX_WALLS]]
        _scenes(res, path, solids)
    res["stage"] = "done"
    res["ok"] = valid
    res["partial"] = bool(loc.partial)
    res["seconds"] = round(time.monotonic() - t0, 2)
    return res


def _scenes(res: dict, path: Path, solids) -> None:
    from ..native.fit import FitError, default_spec_path, load_spec, place
    from ..native.viewer import PALETTE, printed_items, scene_parts

    spec_path = default_spec_path(path)
    placed, spec = [], None
    if spec_path.exists():
        try:
            spec = load_spec(spec_path)
            placed = place(solids, spec, spec_path.parent)
        except FitError as e:
            res["fit_error"] = str(e)
        except Exception as e:  # noqa: BLE001
            res["fit_error"] = f"{type(e).__name__}: {e}"
    names = {p.index: p.name for p in placed if p.index is not None}
    colors = {p.index: p.color for p in placed if p.index is not None and p.color}
    printed = printed_items(solids, names)
    color_of = {label: colors.get(i, PALETTE[i % len(PALETTE)]) for i, (label, _, _) in enumerate(printed)}
    res["scenes"] = [{"name": "As printed", "parts": scene_parts([(l, s, color_of[l]) for l, s, _ in printed])}]
    if placed:
        res["scenes"].append({"name": "Assembled", "parts": scene_parts(
            [(p.name, p.shape, color_of.get(p.name, p.color)) for p in placed])})


# --- server side ------------------------------------------------------------------------------------------

class CheckTimeout(Exception):
    pass


class CheckCrashed(Exception):
    pass


_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None


def _new_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))


def _discard_pool() -> None:
    global _pool
    pool, _pool = _pool, None
    if pool is None:
        return
    for p in list(getattr(pool, "_processes", {}).values()):
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        pass


def check_in_worker(path: Path, min_wall: float = 1.2) -> dict:
    """Run a check in the worker. Raises CheckTimeout or CheckCrashed (the pool is recreated next time)."""
    global _pool
    timeout = check_timeout()
    if not _lock.acquire(timeout=timeout):
        raise CheckTimeout("another check is still running")
    try:
        try:
            if _pool is None:
                _pool = _new_pool()
            try:
                fut = _pool.submit(run_check, str(path), min_wall)
            except (BrokenProcessPool, RuntimeError):  # already dead before we started: replace it, once
                _discard_pool()
                _pool = _new_pool()
                fut = _pool.submit(run_check, str(path), min_wall)
            return fut.result(timeout=timeout)
        except FutureTimeout:
            _discard_pool()
            raise CheckTimeout(f"check timed out after {timeout:g} s") from None
        except BrokenProcessPool:
            _discard_pool()
            raise CheckCrashed("the check worker crashed; try again") from None
    finally:
        _lock.release()


def shutdown() -> None:
    _discard_pool()  # no lock: must not hang at exit behind a running check


atexit.register(shutdown)
