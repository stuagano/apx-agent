"""explain-my-bill-agent: FastAPI app for local dev.

This file is what ``uvicorn app:app`` runs locally — wraps the agent with
the apx-agent A2A protocol surface (``/responses``,
``/.well-known/agent.json``, ``/health``).

MLflow tracing points at the experiment named by ``MLFLOW_EXPERIMENT_NAME``.
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
