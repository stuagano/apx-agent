"""slack-agent Apps-target wrapper.

The existing LlmAgent (with the ``who_am_i`` OBO probe tool) is defined
in ``slack_agent.backend.agent_router``. This module wraps it via
``compile_to_responses_agent`` and registers ``@invoke()`` / ``@stream()``
handlers with ``mlflow.genai.agent_server``.

Run-through for the Slack webhook flow: a Slack slash command hits
``/slack/events`` (mounted in ``start_server.py``), the router validates
the Slack signature, exchanges the Slack user identity for the user's
Databricks token, and invokes the agent over the same FastAPI app's
``/invocations`` route. The agent's tools then run as that Databricks
user via the standard OBO chain.
"""

from __future__ import annotations

import os

from mlflow.genai.agent_server import invoke, stream

from apx_agent import compile_to_responses_agent

from slack_agent.backend.agent_router import agent

MODEL = os.environ.get("APX_MODEL", "databricks-claude-sonnet-4-6")

_invoke_fn, _stream_fn = compile_to_responses_agent(agent, model=MODEL)


@invoke()
def non_streaming(request):
    """Synchronous request handler — /invocations target."""
    return _invoke_fn(request)


@stream()
def streaming(request):
    """Streaming request handler — yields ResponsesAgentStreamEvent chunks."""
    yield from _stream_fn(request)
