"""FastAPI routes for the /_apx/builder visual builder SPA.

Serves the pre-built Vite SPA assets out of the wheel, plus a SPA-fallback
route so client-side routing works (any unmatched path under /_apx/builder/*
serves index.html and lets react-router handle it).
"""
from __future__ import annotations

import importlib.resources as ir
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


def _dist_root() -> Path:
    """Locate the bundled _builder_ui_dist/ directory inside the installed package.

    Returns a real filesystem path via importlib.resources.files(), which works
    for both regular installs and zip-imported wheels (since Python 3.9).
    """
    return Path(str(ir.files("apx_agent").joinpath("_builder_ui_dist")))


def build_builder_router() -> APIRouter:
    """Router that mounts the visual builder SPA at /_apx/builder/*."""
    router = APIRouter()

    @router.get("/_apx/builder", include_in_schema=False)
    @router.get("/_apx/builder/", include_in_schema=False)
    async def builder_index():
        index = _dist_root() / "index.html"
        if not index.exists():
            raise HTTPException(
                status_code=503,
                detail=(
                    "Visual builder assets are not bundled in this install. "
                    "Build them via `cd python/builder-ui && npm run build:dist` "
                    "before installing the wheel."
                ),
            )
        return FileResponse(index, media_type="text/html")

    @router.get("/_apx/builder/{path:path}", include_in_schema=False)
    async def builder_asset(path: str):
        # Asset lookup first; fall back to index.html for SPA deep-link routes.
        target = _dist_root() / path
        if target.is_file():
            return FileResponse(target)
        index = _dist_root() / "index.html"
        if not index.exists():
            raise HTTPException(status_code=404, detail="builder not bundled")
        return FileResponse(index, media_type="text/html")

    return router
