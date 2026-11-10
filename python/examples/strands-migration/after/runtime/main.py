"""DurableAgentServer entrypoint (replaces BedrockAgentCoreApp)."""

import os

import mlflow
import uvicorn
from databricks_agentkit import DurableAgentServer
from databricks_agentkit.runtime import configure_tracing

from runtime.adapter import invoke, recover

# Databricks Apps serves the app on port 8000 and sets PORT; the same default keeps local runs aligned.
DEFAULT_PORT = "8000"

configure_tracing(autolog=mlflow.strands.autolog)  # replaces AgentCore Observability

app = DurableAgentServer()
app.invoke(invoke)
app.recover(recover)


def main() -> None:
    uvicorn.run("runtime.main:app", host="0.0.0.0", port=int(os.getenv("PORT", DEFAULT_PORT)))
