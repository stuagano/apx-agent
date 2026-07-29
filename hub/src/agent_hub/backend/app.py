from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from databricks.sdk import WorkspaceClient
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .._metadata import app_name, dist_dir
from .router import (
    router,
    _AUTO_REGISTER_URLS,
    _crawl_agent,
    _card_from_a2a,
    _AGENTS,
)
from .workspace_discovery import bootstrap_workspace_agents

import logging

logger = logging.getLogger(__name__)


async def _auto_register(app: FastAPI) -> None:
    """Discover workspace Apps agents + crawl AGENT_HUB_AGENT_URLS overlay."""
    ws = getattr(app.state, "workspace_client", None)
    if ws is None:
        logger.warning("No workspace_client on app.state — skipping workspace discovery")
        ws = WorkspaceClient()
        app.state.workspace_client = ws
    try:
        await bootstrap_workspace_agents(
            ws,
            register_from_a2a=_card_from_a2a,
            crawl_agent=_crawl_agent,
            agents=_AGENTS,
            extra_urls=_AUTO_REGISTER_URLS,
        )
    except Exception as e:
        logger.warning("Startup agent discovery failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    app.state.workspace_client = WorkspaceClient()
    # Await discovery so the first /api/agents list already includes workspace peers.
    await _auto_register(app)
    yield


app = FastAPI(title=app_name, lifespan=lifespan)
app.include_router(router)

if dist_dir.exists():
    app.mount("/", StaticFiles(directory=dist_dir, html=True))
