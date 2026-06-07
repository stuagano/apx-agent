"""entity-resolution-agent: FastAPI app for local dev.

The Apps-target deployment uses ``agent_server/start_server.py``. This file
is what ``apx-agent dev`` (or ``uvicorn app:app``) runs locally — adds the React
SPA, the apx-agent dev UI, and the auxiliary ``/api/*`` routes that the SPA calls.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse, RedirectResponse

from apx_agent import create_app
from apx_agent._dev import build_dev_ui_router

from agent import agent
from api import router as api_router

# create_app wires the A2A protocol surface — these routes are always present
# regardless of whether a UI is deployed:
#
#   POST /responses              — invoke the agent (streaming SSE or blocking)
#   GET  /.well-known/agent.json — A2A discovery card (name, description, tools)
#   GET  /health                 — liveness probe
app = create_app(agent)

# Application-specific routes (/api/version, /api/current-user, etc.)
app.include_router(api_router)

# APX dev UI at /_apx/agent — traces, eval, tool editor, setup wizard.
# Present in all environments; safe to include in production.
app.include_router(build_dev_ui_router())

# Optional custom SPA — served at / when a built client is present.
# The A2A protocol routes above work identically whether or not this UI exists.
# ``apx build`` places the compiled React app at client/dist/.
_HERE = Path(__file__).resolve().parent
_CLIENT_DIST = next(
    (
        c
        for c in (_HERE / "client" / "dist", _HERE / "client")
        if (c / "index.html").exists()
    ),
    None,
)

if _CLIENT_DIST is not None:
    # Use explicit GET routes instead of StaticFiles("/") — StaticFiles would
    # intercept POST /responses before the protocol router registers at startup.
    @app.get("/", include_in_schema=False)
    def spa_index():
        return FileResponse(str(_CLIENT_DIST / "index.html"))

    @app.get("/assets/{path:path}", include_in_schema=False)
    def spa_assets(path: str):
        asset = _CLIENT_DIST / "assets" / path
        return FileResponse(str(asset) if asset.is_file() else str(_CLIENT_DIST / "index.html"))
else:
    # No custom UI — redirect / to the APX dev UI.
    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/_apx/agent")
