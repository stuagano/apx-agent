"""Dev-mode FastAPI app — ``uvicorn app:app`` runs the agent locally."""
from __future__ import annotations

from apx_agent import create_app

from agent import agent

app = create_app(agent)
