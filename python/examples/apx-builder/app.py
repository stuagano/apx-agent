"""apx-builder: natural-language agent builder for apx-agent projects.

A FastAPI agent that scaffolds, deploys, and inspects apx-agent projects via
natural language. Tools live in `tools/` (scaffold, deploy, discover, poll) and
the system prompt is in `system_prompt.py`.

Workspace-write tools (`scaffold_project`, `deploy_agent`) go through a
``PolicyGate`` ASK so a human must approve before codegen lands in the
workspace or an App is created.
"""
from __future__ import annotations

from pathlib import Path

from apx_agent import (
    Agent,
    FunctionPolicy,
    PolicyAction,
    PolicyGate,
    PolicyResult,
    create_app,
)
from fastapi.responses import FileResponse

from system_prompt import SYSTEM_PROMPT
from tools.deploy_agent import deploy_agent
from tools.discover_tables import list_genie_spaces, search_tables
from tools.poll_deployment import poll_deployment
from tools.scaffold_project import scaffold_project

_WRITE_TOOLS = frozenset({"scaffold_project", "deploy_agent"})


def _ask_before_workspace_write(event):
    """ASK on scaffold/deploy — human must approve via the approvals UI."""
    if event.tool_name in _WRITE_TOOLS:
        return PolicyResult(
            PolicyAction.ASK,
            reason=(
                f"'{event.tool_name}' writes to the caller's Databricks workspace "
                "or deploys an App — approve only after reviewing the arguments."
            ),
        )
    return None


_write_gate = PolicyGate([
    FunctionPolicy(_ask_before_workspace_write, name="workspace_write_gate"),
])


agent = Agent(
    instructions=SYSTEM_PROMPT,
    tools=[
        search_tables,
        list_genie_spaces,
        scaffold_project,
        deploy_agent,
        poll_deployment,
    ],
    before_tool=_write_gate,
)

app = create_app(agent)

# Serve the React frontend via explicit GET routes.
# DO NOT use app.mount("/", StaticFiles(...)) — it intercepts POST /responses.
_here = Path(__file__).resolve()
_candidates = [
    Path.cwd() / "client" / "dist",
    _here.parent / "client" / "dist",
]
_CLIENT_DIST = next((c for c in _candidates if c.exists()), None)

if _CLIENT_DIST is not None:
    @app.get("/", include_in_schema=False)
    def spa_index():
        return FileResponse(str(_CLIENT_DIST / "index.html"))

    @app.get("/assets/{path:path}", include_in_schema=False)
    def spa_assets(path: str):
        asset = _CLIENT_DIST / "assets" / path
        return FileResponse(str(asset) if asset.is_file() else str(_CLIENT_DIST / "index.html"))
