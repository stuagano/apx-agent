"""data-triage-agent Apps-target wrapper.

The existing agent + 6-step SequentialAgent + deterministic router live in
``data_triage_agent.backend.pipeline``. This module wraps the same `agent`
object for the Databricks Apps runtime via ``compile_to_responses_agent``
and registers ``@invoke()`` / ``@stream()`` handlers with the MLflow
GenAI AgentServer.

The Model Serving deploy path (``apx deploy`` against
``data_triage_agent.backend.pipeline:agent``) keeps working unchanged.
Same agent code; different runtime contract.

Requires `DATA_INSPECTOR_URL` to point at a deployed data-inspector
sub-agent. See `../data-inspector/` for that example.
"""

from __future__ import annotations

import os

from mlflow.genai.agent_server import invoke, stream

from apx_agent import compile_to_responses_agent

# Reuse the existing agent definition — no duplication of pipeline wiring.
from data_triage_agent.backend.pipeline import agent

MODEL = os.environ.get("APX_MODEL", "databricks-claude-sonnet-4-6")

_invoke_fn, _stream_fn = compile_to_responses_agent(agent, model=MODEL)


@invoke()
def non_streaming(request):
    """Synchronous request handler — Databricks Apps /invocations target."""
    return _invoke_fn(request)


@stream()
def streaming(request):
    """Streaming request handler — yields ResponsesAgentStreamEvent chunks."""
    yield from _stream_fn(request)
