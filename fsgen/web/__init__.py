"""`fsgen web`: a small LAN web UI (stdlib HTTP server + static single-page shell).

Route modules are listed in ROUTE_MODULES; slices add their module name to the list.
See fsgen/web/server.py for the route contract and fsgen/web/static/app.js for the tab contract.
"""

ROUTE_MODULES = [
    "fsgen.web.usage_api",
    "fsgen.web.parts_api",
    "fsgen.web.uploads_api",
    "fsgen.web.generate_api",
    "fsgen.web.password_api",
]
