"""`scriptforge web`: a small LAN web UI (stdlib HTTP server + static single-page shell).

Route modules are listed in ROUTE_MODULES; slices add their module name to the list.
See scriptforge/web/server.py for the route contract and scriptforge/web/static/app.js for the tab contract.
"""

ROUTE_MODULES = [
    "scriptforge.web.usage_api",
    "scriptforge.web.parts_api",
    "scriptforge.web.uploads_api",
    "scriptforge.web.generate_api",
    "scriptforge.web.password_api",
]
