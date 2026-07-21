"""FastAPI entry point for Databricks Apps — framework boilerplate.

Don't edit this file. Edit ``../agent.py`` instead.
"""
from __future__ import annotations

import os

from mlflow.genai.agent_server import AgentServer, invoke, stream

from apx_agent import compile_to_responses_agent, mount_mcp_endpoints

from agent import agent

MODEL = os.environ.get("APX_MODEL", "databricks-claude-sonnet-4-6")

_invoke_fn, _stream_fn = compile_to_responses_agent(agent, model=MODEL)


@invoke()
def non_streaming(request):
    """Non-streaming request handler — POST /invocations."""
    return _invoke_fn(request)


@stream()
def streaming(request):
    """Streaming request handler — POST /invocations with stream=true."""
    yield from _stream_fn(request)


server = AgentServer(agent_type="ResponsesAgent")
app = server.app

mount_mcp_endpoints(app, agent)


if __name__ == "__main__":
    server.run("agent_server.start_server:app")
