"""eligibility-agent: FastAPI app for local dev.

This file is what ``uvicorn app:app`` runs locally — wraps the agent
with the apx-agent A2A protocol surface (/responses, /.well-known/agent.json,
/health).
"""
from __future__ import annotations

from apx_agent import create_app

from agent import agent

app = create_app(agent)
