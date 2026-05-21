"""entity-resolution-agent Apps-target wrapper.

The existing HandoffAgent (supervisor + evaluator) is defined in
``entity_resolution_agent.backend.agent_router``. This module wraps it
via ``compile_to_responses_agent`` and registers ``@invoke()`` /
``@stream()`` handlers with ``mlflow.genai.agent_server``.

Same agent code as the Model Serving deploy path; different runtime
contract. The HandoffAgent's message-state scrub (commit 645ba7b7)
ensures the evaluator sub-agent sees a clean conversation tail when
the supervisor hands off.
"""

from __future__ import annotations

import os

from mlflow.genai.agent_server import invoke, stream

from apx_agent import compile_to_responses_agent

# Reuse the existing agent definition — no duplication of supervisor/evaluator
# wiring or vector search tool setup.
from entity_resolution_agent.backend.agent_router import agent

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
