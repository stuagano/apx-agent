"""FastAPI app — uvicorn entry point for ``apx-agent run`` and Databricks Apps hosting."""

from apx_agent import create_app

from agent import agent

app = create_app(agent)
