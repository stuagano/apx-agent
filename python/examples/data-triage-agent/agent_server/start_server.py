"""FastAPI entry point for Databricks Apps — framework boilerplate.

Don't edit this file. Edit ``../agent.py`` instead. This file wires the
agent into the Databricks Apps runtime via a single compile call.

  * Imports ``agent`` from the top-level ``agent.py`` — an
    :class:`apx_agent.KeywordRouter` over the investigation pipeline +
    general fallback.
  * Compiles the whole router once with ``compile_to_responses_agent``;
    the framework recognizes ``KeywordRouter`` natively, so the routing
    decision happens inside the compiled LangGraph (no LLM call) and no
    request-layer dispatch is needed.
  * Mounts apx-agent's ``/mcp`` + ``/.well-known/agent.json`` + ``/health``
    + ``/readyz`` against the same router — Genie / Genie Code consume the SQL /
    lineage / job tools that live on the investigation branch.

Run via ``uvicorn agent_server.start_server:app --host 0.0.0.0 --port $DATABRICKS_APP_PORT``
(driven by ``databricks.yml`` on deploy).
"""

from __future__ import annotations

import os

from mlflow.genai.agent_server import AgentServer, invoke, stream

from apx_agent import compile_to_responses_agent, mount_mcp_endpoints, mount_readyz

from agent import agent

# Keep this fallback aligned with databricks.yml's llm_endpoint_name default.
DEFAULT_MODEL = "databricks-claude-sonnet-4-6"
MODEL = os.environ.get("APX_MODEL", DEFAULT_MODEL)

_invoke, _stream = compile_to_responses_agent(agent, model=MODEL)


@invoke()
def non_streaming(request):
    return _invoke(request)


@stream()
def streaming(request):
    yield from _stream(request)


server = AgentServer(agent_type="ResponsesAgent")
app = server.app

mount_mcp_endpoints(app, agent)
mount_readyz(app, agent, model=MODEL)


if __name__ == "__main__":
    server.run("agent_server.start_server:app")
