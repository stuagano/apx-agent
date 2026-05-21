"""data-inspector: FastAPI app for local dev.

The Apps-target deployment uses ``agent_server/start_server.py``. This file
is what ``apx dev`` (or ``uvicorn app:app``) runs locally — adds CORS so
Genie Code (and other workspace-UI MCP clients) can call ``/mcp`` cross-origin.
"""
from __future__ import annotations

from fastapi.middleware.cors import CORSMiddleware

from apx_agent import create_app

from agent import agent

app = create_app(agent)

# CORS — required for Genie Code (and other workspace-UI MCP clients) to call
# the /mcp endpoint cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://*.cloud.databricks.com",
        "https://*.databricks.com",
    ],
    allow_origin_regex=r"https://.*\.databricks\.com",
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id", "mcp-protocol-version"],
)
