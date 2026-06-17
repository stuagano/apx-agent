"""data-triage-agent: FastAPI app for local dev.

The Apps-target deployment uses ``agent_server/start_server.py``. This file
is what ``apx-agent dev`` (or ``uvicorn app:app``) runs locally — adds the apx-agent dev
UI, the ``/api/*`` routes, the Jira webhook, and CORS for workspace MCP clients.
"""
from __future__ import annotations

import logging
import os

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from apx_agent import create_app
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentConfig

from agent import agent
from api import router as api_router
from integrations.jira.webhook import router as webhook_router

logger = logging.getLogger(__name__)

# Pass config explicitly — pyproject.toml is not included in the .build deploy
# artifact so auto-discovery from __main__.__file__ always fails on Databricks Apps.
_agent_config = AgentConfig(
    name="data_triage_agent",
    description="Investigate why data is missing from Databricks tables or APIs — traces lineage, checks job failures, and inspects source code",
    model="databricks-claude-sonnet-4-6",
    url=os.environ.get("DATA_TRIAGE_AGENT_URL"),
    registry=os.environ.get("AGENT_HUB_URL"),
)

app = create_app(agent, config=_agent_config)
app.include_router(api_router)
app.include_router(webhook_router)

try:
    app.include_router(build_dev_ui_router())
    logger.info("Dev UI mounted at /_apx/agent")
except Exception as e:
    logger.error("Dev UI mount failed: %s", e, exc_info=True)

# CORS — required for Genie Code (and other workspace-UI MCP clients) to call
# the /mcp endpoint cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://*.cloud.databricks.com",
        "https://*.databricks.com",
    ],
    allow_origin_regex=r"https://.*\.databricks\.com",
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id", "mcp-protocol-version"],
)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/_apx/agent")
