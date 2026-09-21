"""sec-10k-agent: FastAPI app for local dev.

``uvicorn app:app`` wraps the SequentialAgent with the apx-agent A2A surface
(``/responses``, ``/.well-known/agent.json``, ``/health``).

Requires ``APX_KA_ENDPOINT_NAME`` pointing at an Agent Bricks Knowledge
Assistant that already indexes 10-K filings.
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
