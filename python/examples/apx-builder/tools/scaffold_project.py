"""Scaffold an apx-agent project into the caller's Databricks Workspace.

Validates ``app_name`` / ``use_case`` before interpolating them into generated
source, so LLM-supplied strings cannot break out of string literals or land
illegal Databricks App names.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.workspace import ImportFormat

from apx_agent import Dependencies

# Databricks Apps names: lowercase alphanumeric + hyphens, 2–63 chars,
# must start/end with alphanumeric. Keep in sync with platform constraints.
_APP_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_USE_CASE_LEN = 500


@dataclass
class GenieSpace:
    id: str
    name: str


def _validate_app_name(app_name: str) -> str:
    """Reject app names that are unsafe to interpolate into generated paths/TOML."""
    if not _APP_NAME_RE.fullmatch(app_name):
        raise ValueError(
            "app_name must be 2–63 chars, lowercase alphanumeric and hyphens, "
            f"starting and ending with alphanumeric; got {app_name!r}"
        )
    return app_name


def _validate_use_case(use_case: str) -> str:
    """Reject use_case strings that break generated Python / TOML literals."""
    if not use_case or not use_case.strip():
        raise ValueError("use_case must be a non-empty description")
    if len(use_case) > _MAX_USE_CASE_LEN:
        raise ValueError(
            f"use_case exceeds {_MAX_USE_CASE_LEN} characters "
            f"(got {len(use_case)})"
        )
    # Block characters that would escape a double-quoted Python/TOML string
    # or inject newlines into generated source.
    if any(ch in use_case for ch in ('"', "\\", "\n", "\r", "\0")):
        raise ValueError(
            'use_case must not contain quotes, backslashes, or newlines'
        )
    return use_case


def _validate_table_name(table: str) -> str:
    """Reject UC three-part names with characters unsafe for codegen."""
    if not re.fullmatch(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){2}", table):
        raise ValueError(
            f"table must be a three-part UC name (catalog.schema.table); got {table!r}"
        )
    return table


def _validate_genie_space(space: GenieSpace) -> GenieSpace:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", space.id):
        raise ValueError(f"genie space id has illegal characters: {space.id!r}")
    if any(ch in space.name for ch in ('"', "\\", "\n", "\r")):
        raise ValueError(
            f"genie space name must not contain quotes/backslashes/newlines: {space.name!r}"
        )
    return space


def _generate_files(
    use_case: str,
    tables: list[str],
    genie_spaces: list[GenieSpace],
    app_name: str,
    include_lineage: bool = False,
) -> dict[str, str]:
    """Generate apx-agent project files. Returns {filename: content}. Pure function — no side effects."""
    use_case = _validate_use_case(use_case)
    app_name = _validate_app_name(app_name)
    tables = [_validate_table_name(t) for t in tables]
    genie_spaces = [_validate_genie_space(s) for s in genie_spaces]

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
from pathlib import Path
from apx_agent import Agent, create_app, {imports_str}
from chainlit.utils import mount_chainlit

agent = Agent(
    tools=[
{tools_str}
    ],
    instructions="You are a data assistant for: {use_case}. Answer questions using the available tools.",
)
app = create_app(agent)
mount_chainlit(app=app, target=str(Path(__file__).parent / "chainlit_app.py"), path="/")
'''

    chainlit_app_py = f'''\
import os
import httpx
import chainlit as cl

_PORT = os.environ.get("DATABRICKS_APP_PORT", "8000")
_API = f"http://localhost:{{_PORT}}/responses"


@cl.on_chat_start
async def start():
    cl.user_session.set("session_id", None)
    cl.user_session.set("history", [])
    await cl.Message(content="Hi! I\\'m your data assistant for: {use_case}. What would you like to know?").send()


@cl.on_message
async def handle(msg: cl.Message):
    session_id = cl.user_session.get("session_id")
    history = cl.user_session.get("history", [])
    history.append({{"role": "user", "content": msg.content}})
    payload = {{"input": history}}
    if session_id:
        payload["session_id"] = session_id
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(_API, json=payload)
        data = r.json()
    cl.user_session.set("session_id", data.get("session_id"))
    output_text = data.get("output_text", "")
    history.append({{"role": "assistant", "content": output_text}})
    cl.user_session.set("history", history)
    await cl.Message(content=output_text).send()
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
chainlit>=2.0.0
fastapi>=0.119.0
uvicorn>=0.37.0
databricks-sdk>=0.74.0
httpx>=0.27.0
'''

    app_yml = '''\
command:
  - uvicorn
  - app:app
  - --host
  - 0.0.0.0
  - --port
  - $DATABRICKS_APP_PORT
  - --workers
  - "1"
env:
  - name: MLFLOW_TRACKING_URI
    value: databricks
  - name: APX_AGENT_MLFLOW_AUTOLOG
    value: "1"
'''

    return {
        "app.py": app_py,
        "chainlit_app.py": chainlit_app_py,
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
    app_name = _validate_app_name(app_name)
    use_case = _validate_use_case(use_case)
    email = ws.current_user.me().user_name
    workspace_path = f"/Users/{email}/apx-builder/{app_name}"
    files = _generate_files(use_case, tables, genie_spaces, app_name, include_lineage)
    _upload_files(ws, files, workspace_path)
    return workspace_path
