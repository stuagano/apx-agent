"""contract-parsing-agent: FastAPI app for local dev.

This file is what ``apx-agent dev`` (or ``uvicorn app:app``) runs locally — wraps
the agent with apx-agent's A2A surface, mounts the SPA + ``/api/*`` routes,
and serves the React client/dist/ when present.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse, RedirectResponse

from apx_agent import create_app

from agent import agent
from api import router

app = create_app(agent)
app.include_router(router)

def _find_client_dist(here: Path, cwd: Path) -> Path | None:
    candidates = [cwd / "client" / "dist"]
    candidates.extend(parent / "client" / "dist" for parent in here.parents)
    return next((candidate for candidate in candidates if candidate.exists()), None)


# CWD is the staged source root in Databricks Apps. Parent fallbacks support
# local editable installs without assuming a fixed checkout depth.
_CLIENT_DIST = _find_client_dist(Path(__file__).resolve(), Path.cwd())

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
