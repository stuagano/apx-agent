from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncio
import logging

from databricks.sdk import WorkspaceClient
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api import router, _AUTO_REGISTER_URLS, _crawl_agent, _card_from_a2a, _AGENTS

app_name = "agent-hub"
# Vite build outputs to __dist__/ (per vite.config.ts).
dist_dir = Path(__file__).resolve().parent / "__dist__"

logger = logging.getLogger(__name__)


async def _auto_register() -> None:
    for url in _AUTO_REGISTER_URLS:
        try:
            a2a = await _crawl_agent(url)
            if a2a:
                existing = _AGENTS.get(a2a.get("name", "").replace("_", "-"))
                card = _card_from_a2a(
                    a2a,
                    url,
                    tags=existing.tags if existing else [],
                )
                _AGENTS[card.id] = card
                logger.info("Auto-registered '%s' from %s (%d tools)", card.id, url, len(card.tools))
            else:
                logger.warning("Could not reach %s — skipping", url)
        except Exception as e:
            logger.warning("Auto-register failed for %s: %s", url, e)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    app.state.workspace_client = WorkspaceClient()
    asyncio.create_task(_auto_register())
    yield


app = FastAPI(title=app_name, lifespan=lifespan)
app.include_router(router)

if dist_dir.exists():
    app.mount("/", StaticFiles(directory=dist_dir, html=True))
