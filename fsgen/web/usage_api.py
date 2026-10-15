"""GET /api/usage: what `fsgen usage` prints, as JSON. Read-only (never writes the baseline or .env)."""
from __future__ import annotations

from ..usage import allocation_status, breakdown
from .server import route


@route("GET", "/api/usage")
def usage(req):
    a = allocation_status()
    purposes, calls = breakdown(a.logged, 12) if a.logged else ([], [])
    return {
        "allocation": a.allocation,
        "used": a.used,
        "left": a.left,
        "percent": round(a.used / a.allocation * 100, 1) if a.allocation else 0.0,
        "logged": len(a.logged),
        "by_purpose": [{"name": k, "count": v} for k, v in purposes],
        "top_endpoints": [{"name": k, "count": v} for k, v in calls],
    }
