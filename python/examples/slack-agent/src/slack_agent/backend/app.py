import logging
import os

from apx_agent import create_app
from apx_agent._models import AgentConfig
from fastapi.responses import RedirectResponse

from .agent_router import agent
from .slack_router import router as slack_router

logger = logging.getLogger(__name__)

_agent_config = AgentConfig(
    name="slack-agent",
    description="Slack bot connected to Databricks via OAuth — illustrates OBO token forwarding",
    model="databricks-claude-sonnet-4-6",
    url=os.environ.get("SLACK_AGENT_URL"),
)

app = create_app(agent, config=_agent_config)
app.include_router(slack_router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/_apx/agent")
