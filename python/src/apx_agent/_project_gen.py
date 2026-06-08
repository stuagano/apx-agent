"""Project generator — writes a deployable Databricks Apps project from an AgentConfig.

Given an ``AgentConfig`` and a target directory, ``generate_project`` writes:

  target_dir/pyproject.toml             — TOML envelope derived from AgentConfig
  target_dir/agent_server/__init__.py   — empty package marker
  target_dir/agent_server/start_server.py — Apps entry-point boilerplate
  target_dir/databricks.yml             — DAB bundle definition
  target_dir/app.yml                    — Apps compute config

The generated project is immediately deployable via ``databricks bundle deploy``.
When ``config.template`` is set the project is config-only (no ``agent.py`` needed)
and ``module = "agent:agent"`` is intentionally omitted from pyproject.toml.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._models import AgentConfig


# ---------------------------------------------------------------------------
# TOML helpers (string-building, no third-party library required)
# ---------------------------------------------------------------------------


def _toml_value(v: Any) -> str:
    """Render a Python value as a TOML inline value."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(i) for i in v) + "]"
    if v is None:
        return '""'
    return str(v)


def _write_toml_section(
    lines: list[str],
    header: str,
    fields: dict[str, Any],
    *,
    skip_none: bool = True,
) -> None:
    """Append a ``[header]`` TOML section to *lines*.

    Fields with ``None`` values are skipped when *skip_none* is True.
    """
    lines.append(f"[{header}]")
    for key, value in fields.items():
        if skip_none and value is None:
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    lines.append("")


# ---------------------------------------------------------------------------
# File content builders
# ---------------------------------------------------------------------------


def _build_pyproject(config: "AgentConfig") -> str:
    """Build pyproject.toml content for *config*."""
    lines: list[str] = []

    # [project]
    lines.append("[project]")
    lines.append(f'name = "{config.name}"')
    lines.append('version = "0.1.0"')
    lines.append('requires-python = ">=3.11"')
    lines.append("dependencies = [")
    lines.append('    "apx-agent[langgraph]",')
    lines.append('    "mlflow[databricks]>=3.12",')
    lines.append("]")
    lines.append("")

    # [tool.apx.agent]
    lines.append("[tool.apx.agent]")
    lines.append(f'name = "{config.name}"')
    if config.description:
        lines.append(f"description = {_toml_value(config.description)}")
    lines.append(f"model = {_toml_value(config.model)}")
    if config.instructions:
        lines.append(f"instructions = {_toml_value(config.instructions)}")

    # Only write module when there is no template — template replaces it
    if not config.template:
        lines.append('module = "agent:agent"')

    if config.examples:
        lines.append(f"examples = {_toml_value(config.examples)}")
    lines.append("")

    # [tool.apx.agent.template] — only when template is set
    if config.template:
        template_dict = dict(config.template)
        _write_toml_section(lines, "tool.apx.agent.template", template_dict, skip_none=False)

    # [tool.apx.agent.memory]
    if config.memory is not None:
        mem = config.memory
        mem_fields: dict[str, Any] = {"type": mem.type}
        if mem.table_name is not None:
            mem_fields["table_name"] = mem.table_name
        mem_fields["auto_create"] = mem.auto_create
        _write_toml_section(lines, "tool.apx.agent.memory", mem_fields)

    # [tool.apx.agent.session]
    if config.session is not None:
        sess = config.session
        sess_fields: dict[str, Any] = {"type": sess.type}
        if sess.table_name is not None:
            sess_fields["table_name"] = sess.table_name
        sess_fields["auto_create"] = sess.auto_create
        _write_toml_section(lines, "tool.apx.agent.session", sess_fields)

    return "\n".join(lines)


_START_SERVER_CONTENT = '''\
"""Databricks Apps entry point — framework boilerplate, do not edit."""
from apx_agent import create_app
from apx_agent._inspection import _load_agent_config

config = _load_agent_config()
app = create_app(config=config)
'''


def _build_databricks_yml(config: "AgentConfig") -> str:
    """Build databricks.yml bundle definition for *config*."""
    name = config.name
    return f"""\
bundle:
  name: {name}

# The root apx-agent ``.gitignore`` excludes ``**/.build/`` because that
# directory is built from source by ``databricks bundle deploy``. DAB sync
# honours ``.gitignore`` when uploading, so ``sync.include`` overrides the
# ignore for the staged tree only.
sync:
  include:
    - .build/**

variables:
  workspace_user:
    description: User who owns the experiment
    default: ${{workspace.current_user.userName}}
  llm_endpoint_name:
    description: Foundation model endpoint the agent calls.
    default: {config.model}
  mlflow_experiment_id:
    description: |
      MLflow experiment ID for tracing. Populate via scripts/quickstart.py
      into a local .env file; surfaced here for the app environment.
    default: ""

artifacts:
  default:
    build: |
      mkdir -p .build
      cp agent.py .build/ 2>/dev/null || true
      cp tools.py .build/ 2>/dev/null || true
      cp -r sub_agents .build/ 2>/dev/null || true
      cp -r agent_server scripts README.md .build/ 2>/dev/null || true
      cp -r .apx-agent .build/ 2>/dev/null || true
      cp -r .apx .build/ 2>/dev/null || true
      cp apx_agent-*.whl .build/ 2>/dev/null || true

resources:
  experiments:
    {name}_experiment:
      name: /Users/${{var.workspace_user}}/${{bundle.name}}-${{bundle.target}}

  apps:
    {name}:
      name: {name}
      description: {name} apx-agent
      source_code_path: ./.build
      user_api_scopes:
        - sql
        - serving.serving-endpoints
      resources:
        - name: experiment
          experiment:
            experiment_id: ${{resources.experiments.{name}_experiment.id}}
            permission: CAN_MANAGE
        - name: llm-endpoint
          description: Foundation model endpoint used by the agent.
          serving_endpoint:
            name: ${{var.llm_endpoint_name}}
            permission: CAN_QUERY
      config:
        command:
          - uvicorn
          - agent_server.start_server:app
          - --host
          - 0.0.0.0
          - --port
          - $DATABRICKS_APP_PORT
        env:
          - name: APX_MODEL
            value: ${{var.llm_endpoint_name}}
          - name: MLFLOW_TRACKING_URI
            value: databricks
          - name: MLFLOW_EXPERIMENT_ID
            value: ${{var.mlflow_experiment_id}}
          - name: APX_AGENT_MLFLOW_AUTOLOG
            value: "1"

targets:
  dev:
    mode: development
    default: true
  prod:
    mode: production
    resources:
      apps:
        {name}:
          name: {name}
"""


def _build_app_yml(config: "AgentConfig") -> str:
    """Build app.yml compute config for *config*."""
    return """\
command:
  - uvicorn
  - agent_server.start_server:app
  - --host
  - 0.0.0.0
  - --port
  - $DATABRICKS_APP_PORT
env:
  - name: MLFLOW_TRACKING_URI
    value: databricks
  - name: APX_AGENT_MLFLOW_AUTOLOG
    value: "1"
"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_project(config: "AgentConfig", target_dir: Path) -> None:
    """Write a deployable Databricks Apps project to *target_dir*.

    *target_dir* is created if it does not exist. Any files that already exist
    in the directory are overwritten.

    Args:
        config: Validated ``AgentConfig`` describing the agent.
        target_dir: Destination directory for the generated project.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    # pyproject.toml
    (target_dir / "pyproject.toml").write_text(_build_pyproject(config))

    # agent_server/
    agent_server_dir = target_dir / "agent_server"
    agent_server_dir.mkdir(exist_ok=True)
    (agent_server_dir / "__init__.py").write_text("")
    (agent_server_dir / "start_server.py").write_text(_START_SERVER_CONTENT)

    # databricks.yml
    (target_dir / "databricks.yml").write_text(_build_databricks_yml(config))

    # app.yml
    (target_dir / "app.yml").write_text(_build_app_yml(config))
