"""DurableAgentServer entrypoint (replaces rca/server.py)."""

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from databricks_agentkit import DurableAgentServer
from databricks_agentkit.langgraph import configure_tracing
from runtime.adapter import invoke, recover

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)
configure_tracing()

# Databricks Apps serves the app on port 8000 and sets PORT; the same default keeps local runs aligned.
DEFAULT_PORT = "8000"

app = DurableAgentServer()
app.invoke(invoke)
app.recover(recover)


def main() -> None:
    uvicorn.run("runtime.main:app", host="0.0.0.0", port=int(os.getenv("PORT", DEFAULT_PORT)))
