from pathlib import Path

from apx_agent import create_app
from apx_agent._dev import build_dev_ui_router
from fastapi.responses import FileResponse, RedirectResponse

from .agent_router import agent
from .router import router

app = create_app(agent)
app.include_router(router)
app.include_router(build_dev_ui_router())

# Locate client/dist.  CWD is the source root in Databricks Apps.
# Fall back through __file__-relative parents for local editable-install runs.
_here = Path(__file__).resolve()
_candidates = [
    Path.cwd() / "client" / "dist",
    _here.parents[3] / "client" / "dist",
    _here.parents[4] / "client" / "dist",
    _here.parents[5] / "client" / "dist",
    _here.parents[6] / "client" / "dist",
]
_CLIENT_DIST = next((c for c in _candidates if c.exists()), None)

if _CLIENT_DIST is not None:
    # Use explicit GET-only routes instead of app.mount("/", StaticFiles).
    # StaticFiles mounted at "/" intercepts POST /responses because it is
    # added at module load time, before the protocol router registers its
    # routes during lifespan startup.  Explicit GET routes do not intercept
    # POST requests so /responses works correctly.
    @app.get("/", include_in_schema=False)
    def spa_index():
        return FileResponse(str(_CLIENT_DIST / "index.html"))

    @app.get("/assets/{path:path}", include_in_schema=False)
    def spa_assets(path: str):
        asset = _CLIENT_DIST / "assets" / path
        return FileResponse(str(asset) if asset.is_file() else str(_CLIENT_DIST / "index.html"))
else:
    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/_apx/agent")
