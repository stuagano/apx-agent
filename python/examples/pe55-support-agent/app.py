"""pe55-support-agent: FastAPI app for local dev.

``uvicorn app:app`` wraps the declared Agent with the apx-agent A2A surface
(``/responses``, ``/.well-known/agent.json``, ``/health``).

Set ``APX_SMOKE_MODE=1`` to run without go/e2dogfood. Live mode needs a
CLI profile that can see ``agent_cuj`` on that workspace.
"""
from __future__ import annotations

import os

import mlflow

from apx_agent import create_app

from agent import agent

_experiment = os.environ.get("MLFLOW_EXPERIMENT_NAME")
if _experiment:
    mlflow.set_experiment(_experiment)

app = create_app(agent)
