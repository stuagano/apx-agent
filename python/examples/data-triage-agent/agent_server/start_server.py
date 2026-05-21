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

# Importing the agent module triggers the @invoke()/@stream() decorators
# and produces the ``investigation`` + ``general`` branch agents — both
# regular apx-agent BaseAgent instances.
from agent_server import agent as _agent_mod  # noqa: F401  (import for side effects)

from apx_agent import mount_mcp_endpoints

server = AgentServer(agent_type="ResponsesAgent")
app = server.app

# Mount apx-agent's MCP surface on the same FastAPI app. We pass the
# investigation pipeline (the SequentialAgent) — Genie / Genie Code
# consume tools, and the investigation branch is where the SQL / lineage
# / job tools live. The general LlmAgent branch is a fallback for
# non-investigation queries handled server-side by the @invoke/@stream
# router; it doesn't need a separate MCP surface.
mount_mcp_endpoints(app, _agent_mod._router.investigation)


if __name__ == "__main__":
    server.run("agent_server.start_server:app")
