"""data-inspector Apps-target wrapper.

The existing LlmAgent (SQL + Delta forensics tools) is defined in
``data_inspector.backend.agent_router``. This module wraps it via
``compile_to_responses_agent`` and registers ``@invoke()`` / ``@stream()``
handlers with ``mlflow.genai.agent_server``.

Replaces the legacy ``create_app(agent)`` deploy path. Same agent code;
new contract (ResponsesAgent for Apps target).
"""

from __future__ import annotations

import os

from mlflow.genai.agent_server import invoke, stream

from apx_agent import compile_to_responses_agent

from data_inspector.backend.agent_router import agent

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
