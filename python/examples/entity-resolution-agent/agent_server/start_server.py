"""FastAPI entry point for Databricks Apps.

Hosts the agent on both protocol surfaces from one app:

  * ``/invocations`` + ``/responses`` — Mosaic AI Apps shape via
    ``mlflow.genai.agent_server.AgentServer``. Drives ``@invoke()`` /
    ``@stream()`` handlers defined in ``agent_server.agent``.
  * ``/mcp`` (+ ``/mcp/sse``, ``/.well-known/agent.json``, ``/health``) —
    apx-agent's MCP surface so Genie / Genie Code can consume the same
    agent as an MCP source. Mounted via ``apx_agent.mount_mcp_endpoints``.

Run via ``uvicorn agent_server.start_server:app --host 0.0.0.0 --port $DATABRICKS_APP_PORT``.
"""

from __future__ import annotations

from mlflow.genai.agent_server import AgentServer

from agent_server import agent as _agent_mod  # noqa: F401  (import for side effects)

from apx_agent import mount_mcp_endpoints

server = AgentServer(agent_type="ResponsesAgent")
app = server.app

# Mount apx-agent's MCP surface so Genie / Genie Code can consume the
# entity-resolution tools (vector search, supervisor logic, evaluator).
mount_mcp_endpoints(app, _agent_mod.agent)


if __name__ == "__main__":
    server.run("agent_server.start_server:app")
