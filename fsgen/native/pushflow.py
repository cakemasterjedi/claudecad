"""Shared push flow for `fsgen studio push` and the web UI: estimate (0 calls) and push of a traced script."""
from __future__ import annotations

from pathlib import Path


def estimate(tr, name: str, document: str | None = None, fresh: bool = False, metrics: bool = False,
             exports: tuple = ()):
    """-> (Estimate, state or None). Offline: the saved state comes from the local state file, 0 Onshape calls."""
    from ..cloud import known_target
    from ..usage import estimate_push
    from . import build as nb

    tgt = known_target(document)
    st0 = None if fresh or tgt is None else nb.find_state(name, tgt["did"], tgt["wid"])
    return estimate_push(tr, st0, metrics=metrics, exports=exports), st0


def push(o, tr, name: str, script_text: str, source: str, document: str | None = None, public: bool | None = None,
         fresh: bool = False, on_learned=None):
    """Push traced script `tr` into the target workspace as Part Studio `name` and record the outcome in the
    feature catalog. `o` is an Onshape client. -> (PushResult, studio state). `on_learned(names)` is called with
    catalog features learned from a successful push."""
    from ..cloud import target_workspace
    from . import build as nb, catalog

    ws = target_workspace(o, document, public=public)
    st = nb.get_studio(o, name, ws["did"], ws["wid"], fresh=fresh)
    r = nb.push_trace(o, st, tr)
    if r.ok:
        learned = catalog.record_success(tr.features, script_text, source)
        if learned and on_learned:
            on_learned(learned)
    elif r.failed:
        f = next((f for f in tr.features if f.name == r.failed), None)
        if f is not None:
            catalog.record_failure(f.feature_type, r.error or "", source)
    return r, st
