import base64
from dataclasses import dataclass
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.workspace import ImportFormat
from apx_agent import Dependencies


@dataclass
class GenieSpace:
    id: str
    name: str


def _generate_files(
    use_case: str,
    tables: list[str],
    genie_spaces: list[GenieSpace],
    app_name: str,
    include_lineage: bool = False,
) -> dict[str, str]:
    """Generate apx-agent project files. Returns {filename: content}. Pure function — no side effects."""
    tool_imports = []
    tool_calls = []

    if tables:
        tool_imports.append("sql_tool")
        for table in tables:
            tool_calls.append(f'    sql_tool("{table}"),')

    if genie_spaces:
        tool_imports.append("genie_tool")
        for space in genie_spaces:
            tool_calls.append(f'    genie_tool("{space.id}"),  # {space.name}')

    if include_lineage:
        tool_imports.append("lineage_tool")
        tool_calls.append("    lineage_tool(),")

    imports_str = ", ".join(tool_imports)
    tools_str = "\n".join(tool_calls)

    app_py = f'''\
from apx_agent import Agent, create_app, {imports_str}

agent = Agent(
    tools=[
{tools_str}
    ],
    instructions="You are a data assistant for: {use_case}. Answer questions using the available tools.",
)
app = create_app(agent)
'''

    pyproject_toml = f'''\
[project]
name = "{app_name}"
requires-python = ">=3.11"
dependencies = [
    "apx-agent @ git+https://github.com/stuagano/apx-agent.git#subdirectory=python",
]

[tool.apx.agent]
name = "{app_name}"
description = "{use_case}"
model = "databricks-claude-sonnet-4-6"
url = ""

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
'''

    # requirements.txt alongside pyproject.toml for Databricks Apps pip fallback
    requirements_txt = '''\
apx-agent @ git+https://github.com/stuagano/apx-agent.git#subdirectory=python
fastapi>=0.119.0
uvicorn>=0.37.0
databricks-sdk>=0.74.0
httpx>=0.27.0
'''

    app_yml = '''\
command:
  - uvicorn
  - app:app
  - --port
  - $DATABRICKS_APP_PORT
  - --workers
  - "1"
'''

    return {
        "app.py": app_py,
        "pyproject.toml": pyproject_toml,
        "requirements.txt": requirements_txt,
        "app.yml": app_yml,
    }


def _upload_files(ws: WorkspaceClient, files: dict[str, str], workspace_path: str) -> None:
    """Upload project files to the Databricks Workspace."""
    ws.workspace.mkdirs(workspace_path)
    for filename, content in files.items():
        ws.workspace.import_(
            path=f"{workspace_path}/{filename}",
            content=base64.b64encode(content.encode()).decode(),
            format=ImportFormat.AUTO,
            overwrite=True,
        )


def scaffold_project(
    use_case: str,
    tables: list[str],
    genie_spaces: list[GenieSpace],
    app_name: str,
    include_lineage: bool,
    ws: Dependencies.UserClient,
) -> str:
    """Scaffold an apx-agent project in the Databricks Workspace. Returns the workspace path."""
    email = ws.current_user.me().user_name
    workspace_path = f"/Users/{email}/apx-builder/{app_name}"
    files = _generate_files(use_case, tables, genie_spaces, app_name, include_lineage)
    _upload_files(ws, files, workspace_path)
    return workspace_path
