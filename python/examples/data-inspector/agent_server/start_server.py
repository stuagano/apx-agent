"""FastAPI entry point for Databricks Apps.

Hosts the agent on both protocol surfaces from one app:

  * ``/invocations`` + ``/responses`` — Mosaic AI Apps shape via
    ``mlflow.genai.agent_server.AgentServer``.
  * ``/mcp`` (+ ``/mcp/sse``, ``/.well-known/agent.json``, ``/health``) —
    apx-agent's MCP surface so Genie / Genie Code / data-triage-agent
    can consume the same agent over the MCP protocol or A2A.

Run via ``uvicorn agent_server.start_server:app --host 0.0.0.0 --port $DATABRICKS_APP_PORT``.
"""

from __future__ import annotations

from mlflow.genai.agent_server import AgentServer

from agent_server import agent as _agent_mod  # noqa: F401  (import for side effects)

from apx_agent import mount_mcp_endpoints

server = AgentServer(agent_type="ResponsesAgent")
app = server.app

# Mount the apx-agent MCP surface so data-triage-agent (and Genie) can
# consume the inspector's tools — list_catalogs/schemas/tables,
# describe_table, get_delta_history, sample_rows, count_rows, etc.
mount_mcp_endpoints(app, _agent_mod.agent)


if __name__ == "__main__":
    server.run("agent_server.start_server:app")
