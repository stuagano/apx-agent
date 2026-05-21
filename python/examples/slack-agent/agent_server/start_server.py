"""FastAPI entry point for Databricks Apps.

Hosts the slack-agent on THREE protocol surfaces from one app:

  * ``POST /invocations`` + ``POST /responses`` — Mosaic AI Apps shape
    via ``mlflow.genai.agent_server.AgentServer``. Driven by ``@invoke()``
    / ``@stream()`` handlers in ``agent_server.agent``.
  * ``POST /slack/events`` + ``GET /slack/install`` + ``GET /slack/oauth/callback``
    — Slack webhook surface mounted from the existing ``slack_router``.
    Validates Slack signatures, runs OAuth, exchanges Slack identities
    for Databricks tokens, then delegates to the agent.
  * ``/mcp`` (+ ``/mcp/sse``, ``/.well-known/agent.json``, ``/health``) —
    apx-agent's MCP surface so Genie / Genie Code can also consume the
    same agent (sans Slack-specific routing).

Run via ``uvicorn agent_server.start_server:app --host 0.0.0.0 --port $DATABRICKS_APP_PORT``.
"""

from __future__ import annotations

from mlflow.genai.agent_server import AgentServer

from agent_server import agent as _agent_mod  # noqa: F401  (import for side effects)

from apx_agent import mount_mcp_endpoints
from slack_agent.backend.slack_router import router as _slack_router

server = AgentServer(agent_type="ResponsesAgent")
app = server.app

# Mount the Slack webhook routes on the same FastAPI app — slack-agent's
# raison d'être. Slack hits these directly; the routes ultimately
# invoke the agent via the same app's /invocations surface.
app.include_router(_slack_router)

# Mount apx-agent's MCP surface so Genie / Genie Code can also consume
# the agent's tools (just `who_am_i` today; the Slack-side flow stays
# separate).
mount_mcp_endpoints(app, _agent_mod.agent)


if __name__ == "__main__":
    server.run("agent_server.start_server:app")
