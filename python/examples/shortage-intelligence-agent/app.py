import logging
import os

from apx_agent import create_app
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentConfig
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from agent import agent
from config import get_settings
from api import router

logger = logging.getLogger(__name__)

_settings = get_settings()

_agent_config = AgentConfig(
    name="shortage_intelligence_agent",
    description="Detects shortage signals in demand data, validates against market reports, checks live vendor pricing, and delivers actionable reports to sourcing and sales teams",
    model=_settings.agent_model,
    url=os.environ.get("SHORTAGE_AGENT_URL"),
    registry=os.environ.get("AGENT_HUB_URL"),
)

app = create_app(agent, config=_agent_config)
app.include_router(router)

try:
    app.include_router(build_dev_ui_router())
    logger.info("Dev UI mounted at /_apx/agent")
except Exception as e:
    logger.error("Dev UI mount failed: %s", e, exc_info=True)

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
