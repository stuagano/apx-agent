from apx_agent import create_app
from fastapi.middleware.cors import CORSMiddleware

from .agent_router import agent

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
