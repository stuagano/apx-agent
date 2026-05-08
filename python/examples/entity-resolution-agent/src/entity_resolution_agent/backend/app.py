from pathlib import Path

from apx_agent import create_app
from apx_agent._dev import build_dev_ui_router
from fastapi.responses import FileResponse, RedirectResponse

from .agent_router import agent
from .router import router

# create_app wires the A2A protocol surface — these routes are always present
# regardless of whether a UI is deployed:
#
#   POST /responses              — invoke the agent (streaming SSE or blocking)
#   GET  /.well-known/agent.json — A2A discovery card (name, description, tools)
#   GET  /health                 — liveness probe
#
# That's enough for an agent hub, orchestrator, or any HTTP client to discover
# and call this agent without a browser.  The UI added below is optional.
app = create_app(agent)

# Application-specific routes (/api/version, /api/current-user, etc.)
app.include_router(router)

# APX dev UI at /_apx/agent — traces, eval, tool editor, setup wizard.
# Present in all environments; safe to include in production.
app.include_router(build_dev_ui_router())

# ---------------------------------------------------------------------------
# Optional custom SPA — served at / when a built client is present.
# The A2A protocol routes above work identically whether or not this UI exists.
#
# apx build places the compiled React app at .build/client/dist/ or .build/client/
# depending on version.  Check for index.html to distinguish a real build from
# an empty directory.
# ---------------------------------------------------------------------------
_here = Path(__file__).resolve()
_candidates = [
    Path.cwd() / "client" / "dist",
    Path.cwd() / "client",
    _here.parents[3] / "client" / "dist",
    _here.parents[3] / "client",
    _here.parents[4] / "client" / "dist",
    _here.parents[4] / "client",
    _here.parents[5] / "client" / "dist",
    _here.parents[5] / "client",
]
_CLIENT_DIST = next((c for c in _candidates if (c / "index.html").exists()), None)

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
