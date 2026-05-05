from pathlib import Path

from apx_agent import Agent, create_app
from fastapi.responses import FileResponse

from system_prompt import SYSTEM_PROMPT
from tools.discover_tables import list_genie_spaces, search_tables
from tools.deploy_agent import deploy_agent
from tools.poll_deployment import poll_deployment
from tools.scaffold_project import scaffold_project

agent = Agent(
    tools=[search_tables, list_genie_spaces, scaffold_project, deploy_agent, poll_deployment],
    instructions=SYSTEM_PROMPT,
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
