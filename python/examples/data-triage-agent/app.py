"""data-triage-agent — native DurableAgentServer entrypoint for Databricks Apps.

One entrypoint for both local dev (``uvicorn app:app`` / ``apx-agent dev``) and
the deployed App. ``apx_agent._serve.create_app`` compiles the declared
``target = "durable_agent_server"`` runtime (durable ``/invocations`` +
``/readyz``; stateless per request, no managed session store since the root is
a ``KeywordRouter``; invocation events persist in the runtime store, and
request-user invocations are not recovered in the background); on top of it we
re-mount the surface this
example needs:

  * ``mount_mcp_endpoints`` — ``/mcp`` + ``/.well-known/agent.json`` + ``/health``
    + root chat + dev UI. Genie / Genie Code discover the SQL / lineage / job
    tools at ``/mcp`` (hence the ``mcp-`` app name).
  * CORS — so workspace-UI MCP clients can call ``/mcp`` cross-origin.
  * ``/api/*`` app routes + the Jira webhook.
"""
from __future__ import annotations

import logging
import os

from fastapi.middleware.cors import CORSMiddleware

from apx_agent import mount_mcp_endpoints
from apx_agent._serve import create_app

from agent import agent
from api import router as api_router
from integrations.jira.webhook import router as webhook_router

logger = logging.getLogger(__name__)

app = create_app()

mount_mcp_endpoints(app, agent)
app.include_router(api_router)
app.include_router(webhook_router)

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["DATABRICKS_APP_PORT"]))
