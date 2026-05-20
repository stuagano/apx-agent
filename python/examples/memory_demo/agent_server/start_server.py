"""FastAPI entry point for Databricks Apps.

Databricks Apps will run this file via the command in ``databricks.yml``::

    uvicorn agent_server.start_server:app --host 0.0.0.0 --port $DATABRICKS_APP_PORT

Importing ``agent_server.agent`` triggers the ``@invoke()`` / ``@stream()``
decorators that register the request handlers with the AgentServer
singleton. The ``ResponsesAgent`` agent_type wires the MLflow validators
and streaming-trace aggregation automatically.
"""

from __future__ import annotations

from mlflow.genai.agent_server import AgentServer

# Importing the agent module triggers the @invoke()/@stream() decorators,
# which register the handlers on the global AgentServer state. This MUST
# happen before AgentServer() is constructed.
from agent_server import agent  # noqa: F401  (import for side effects)

server = AgentServer(agent_type="ResponsesAgent")
app = server.app


if __name__ == "__main__":
    # Local-dev entry point: `python -m agent_server.start_server`
    # honors PORT / WORKERS / RELOAD CLI args. Production runs via the
    # uvicorn command in databricks.yml.
    server.run("agent_server.start_server:app")
