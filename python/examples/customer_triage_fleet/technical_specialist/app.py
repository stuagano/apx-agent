"""FastAPI app — uvicorn entry point for local dev."""
from apx_agent import create_app

from agent import agent

app = create_app(agent)
