"""FastAPI entry point for Databricks Apps — framework boilerplate.

Don't edit this file. Edit ``../agent.py`` (which re-exports the router
from ``data_triage_agent.backend.pipeline``) instead. This file wires
that agent into the Databricks Apps runtime:

  * Imports the user's ``agent`` from the top-level ``agent.py`` — a
    ``_DeterministicRouter`` exposing ``.investigation`` and
    ``.general`` branches.
  * ``compile_to_responses_agent`` doesn't recognize the custom
    ``_DeterministicRouter`` ``BaseAgent`` subclass, so we compile its
    two underlying branches separately and do the keyword routing at the
    request layer. Identical semantics to the Model-Serving path, just
    moved one layer up.
  * Registers ``@invoke`` / ``@stream`` handlers that dispatch via
    ``_DeterministicRouter.matches_investigation`` on the latest user
    message.
  * Mounts apx-agent's ``/mcp`` + ``/.well-known/agent.json`` + ``/health``
    against the investigation pipeline — Genie / Genie Code consume the
    SQL / lineage / job tools that live on that branch, not on the
    general fallback agent.

Run via ``uvicorn agent_server.start_server:app --host 0.0.0.0 --port $DATABRICKS_APP_PORT``
(driven by ``databricks.yml`` on deploy).
"""

from __future__ import annotations

import os

from mlflow.genai.agent_server import AgentServer, invoke, stream

from apx_agent import compile_to_responses_agent, mount_mcp_endpoints

# Import the user's agent (the deterministic router) from the top-level
# module + the router subclass for the dispatch hook.
from agent import agent
from data_triage_agent.backend.pipeline import _DeterministicRouter

assert isinstance(agent, _DeterministicRouter)

MODEL = os.environ.get("APX_MODEL", "databricks-claude-sonnet-4-6")

# Compile each branch separately. ``compile_to_responses_agent`` recognizes
# the underlying ``SequentialAgent`` + ``LlmAgent`` shapes directly.
_invest_invoke, _invest_stream = compile_to_responses_agent(
    agent.investigation, model=MODEL,
)
_general_invoke, _general_stream = compile_to_responses_agent(
    agent.general, model=MODEL,
)


def _route_text(request) -> str:
    """Extract the latest user message for keyword routing.

    The MLflow GenAI request object's ``input`` is a list of message-like
    objects; we walk it in reverse and pull the first ``role == "user"``
    content payload. Accepts both pydantic instances and plain dicts so
    callers can hand in either shape.
    """
    items = getattr(request, "input", None) or []
    for item in reversed(list(items)):
        role = getattr(item, "role", None) or (
            item.get("role") if isinstance(item, dict) else None
        )
        if role == "user":
            content = (
                getattr(item, "content", None)
                or (item.get("content") if isinstance(item, dict) else None)
                or ""
            )
            return str(content)
    return ""


@invoke()
def non_streaming(request):
    """Route to the investigation pipeline on keyword match, else general."""
    if _DeterministicRouter.matches_investigation(_route_text(request)):
        return _invest_invoke(request)
    return _general_invoke(request)


@stream()
def streaming(request):
    """Route to the investigation pipeline on keyword match, else general."""
    if _DeterministicRouter.matches_investigation(_route_text(request)):
        yield from _invest_stream(request)
    else:
        yield from _general_stream(request)


server = AgentServer(agent_type="ResponsesAgent")
app = server.app

# Mount apx-agent's MCP surface against the investigation pipeline — Genie /
# Genie Code consume tools, and the investigation branch is where the SQL /
# lineage / job tools live. The general LlmAgent branch is a server-side
# fallback for non-investigation queries; it doesn't need a separate MCP
# surface.
mount_mcp_endpoints(app, agent.investigation)


if __name__ == "__main__":
    server.run("agent_server.start_server:app")
