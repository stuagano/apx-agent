"""Databricks Apps host selector — framework boilerplate, do not edit.

The only Apps host is DurableAgentServer.
"""
from __future__ import annotations

import os


def main() -> None:
    os.environ.setdefault("APX_RUNTIME", "apps")
    os.execvp(
        "uvicorn",
        ["uvicorn", "agent_server.start_server:app", "--host", "0.0.0.0", "--port", "8000"],
    )
