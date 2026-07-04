"""Project generator — writes a deployable Databricks Apps project from an AgentConfig.

Given an ``AgentConfig`` and a target directory, ``generate_project`` writes:

  target_dir/pyproject.toml             — TOML envelope derived from AgentConfig
  target_dir/agent_server/__init__.py   — empty package marker
  target_dir/agent_server/start_server.py — Apps entry-point boilerplate
  target_dir/databricks.yml             — DAB bundle definition
  target_dir/app.yml                    — Apps compute config

The generated project is immediately deployable via ``databricks bundle deploy``.
The agent always has a single Python definition: when ``config.template`` is set,
``render_agent_py`` codegens a top-level ``agent.py`` that constructs the template
agent in code (ADK-style), and ``module = "agent:agent"`` is always written.
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


def _toml_value_nested(v: Any) -> str:
    """Render a Python value as a TOML inline value, including nested dicts.

    Extends ``_toml_value`` with dict support so that tool tables with nested
    structures (e.g. MCP tool args lists) serialise correctly.

    :param v: Python value to render.
    :returns: A TOML inline representation of *v*.
    """
    if isinstance(v, dict):
        pairs = ", ".join(f"{k} = {_toml_value_nested(val)}" for k, val in v.items())
        return "{" + pairs + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value_nested(i) for i in v) + "]"
    return _toml_value(v)


def _build_pyproject(config: "AgentConfig") -> str:
    """Build pyproject.toml content for *config*.

    Serializes all ``AgentConfig`` fields that have TOML equivalents.
    ``[[tool.apx.tools]]`` array-of-tables entries are appended for each entry
    in ``config.tools`` and for each ``SkillConfig`` in ``config.skills``.

    :param config: Validated ``AgentConfig`` to serialize.
    :returns: Complete pyproject.toml content as a string.
    """
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
    # sub_agents — without this the runtime envelope merge never sees the
    # spec's sub-agent URLs and the deployed app silently has no A2A tools.
    if config.sub_agents:
        lines.append(f"sub_agents = {_toml_value(config.sub_agents)}")

    # knowledge — emit only when the caller explicitly set it in the AgentConfig.
    # The auto-default "./.apx/okf" was removed: generate_project writes no .apx/
    # artifact, so auto-emitting a knob pointing at a non-existent bundle would be
    # incoherent. The bundle-writing path (apx-agent scaffold) emits the knob there
    # instead, gated on manifest-is-not-None, so knob ⟺ bundle is always satisfied.
    if config.knowledge is not None:
        lines.append(f"knowledge = {_toml_value(config.knowledge)}")

    # Python-canonical: the agent always has a top-level agent.py (generated from
    # the template by render_agent_py), so the module spec is always written and
    # the [tool.apx.agent.template] section is no longer emitted.
    lines.append('module = "agent:agent"')

    if config.examples:
        lines.append(f"examples = {_toml_value(config.examples)}")
    lines.append("")

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

    # [[tool.apx.tools]] — one array entry per tool declared in the YAML
    for tool_dict in config.tools:
        lines.append("")
        lines.append("[[tool.apx.tools]]")
        for k, v in tool_dict.items():
            lines.append(f"{k} = {_toml_value_nested(v)}")

    # [[tool.apx.tools]] — one skill entry per SkillConfig; path is project-relative
    for skill in config.skills:
        lines.append("")
        lines.append("[[tool.apx.tools]]")
        lines.append(f'type = "skill"')
        lines.append(f"name = {_toml_value(skill.name)}")
        lines.append(f"description = {_toml_value(skill.description)}")
        lines.append(f'path = "skills/{skill.name}.md"')

    return "\n".join(lines)


_START_SERVER_CONTENT = '''\
"""Databricks Apps entry point — framework boilerplate, do not edit."""
from apx_agent import create_app
from apx_agent._inspection import _load_agent_config

from agent import agent

config = _load_agent_config()
app = create_app(agent=agent, config=config)
'''

# name -> (import module, class). Built-in templates whose ``build()`` is a pure
# spec->constructor forward, so the generated agent.py reproduces the same agent.
_TEMPLATE_TARGETS = {
    "coworker": ("apx_agent.coworker", "CoworkerAgent"),
    "data": ("apx_agent.data_agent", "DataAgent"),
}


def render_agent_py(config: "AgentConfig") -> str:
    """Codegen a top-level ``agent.py`` that builds the template agent in code.

    The YAML/config deploy path used to ship a config-only project (the runtime
    rebuilt the agent from ``[tool.apx.agent.template]`` via the registry). This
    emits an ADK-style ``agent = CoworkerAgent("cat", "sch", persona=...)`` instead
    so the agent has a single Python definition — editable, debuggable, and visible
    in the dev-UI Edit page. The template's ``build()`` is a pure spec->kwargs
    forward (see coworker.py / data_agent.py), so the constructed agent is identical.

    ``ws`` is intentionally omitted: at import time there is no workspace client,
    and DataAgent/CoworkerAgent fall back to the baked ``.apx/schema.json`` for
    grounding (ws is resolved by the serving runtime later).

    :param config: Validated ``AgentConfig`` whose ``template`` selects the agent.
    :returns: Complete ``agent.py`` source.
    """
    if config.template is None:
        raise ValueError("render_agent_py requires config.template to be set")
    name = config.template.get("name")
    spec = {k: v for k, v in config.template.items() if k != "name"}
    if config.knowledge is not None and "knowledge" not in spec:
        spec["knowledge"] = config.knowledge

    target = _TEMPLATE_TARGETS.get(name or "")
    if target is None:
        # Unknown/third-party template: fall back to a faithful registry build
        # call rather than guessing a constructor we don't know.
        return (
            "from apx_agent._template import template_registry\n\n"
            f"agent = template_registry.build({name!r}, {spec!r})\n"
        )

    module, cls = target
    catalog = spec.pop("catalog", None)
    schema = spec.pop("schema", None)
    spec.setdefault("name", config.name)
    pos = [repr(v) for v in (catalog, schema) if v is not None]
    kw = [f"{k}={v!r}" for k, v in spec.items() if v is not None]
    args = ", ".join(pos + kw)
    return f"from {module} import {cls}\n\nagent = {cls}({args})\n"

_KEEPALIVE_CONTENT = '''\
"""Scheduled keepalive — pings /readyz to prevent cold starts. Do not edit."""
import sys
import urllib.request

url = sys.argv[1]
try:
    urllib.request.urlopen(url, timeout=10)
    print(f"OK: {url}")
except Exception as exc:
    print(f"WARN: {url} — {exc}")
'''


def _build_databricks_yml(config: "AgentConfig") -> str:
    """Build databricks.yml bundle definition for *config*.

    Includes a ``cp -r skills .build/`` step when the config declares skills so
    that the skill markdown files land in the deployed app's source root.

    :param config: Validated ``AgentConfig`` describing the agent.
    :returns: Complete ``databricks.yml`` content as a string.
    """
    name = config.name
    skills_copy = "      cp -r skills .build/ 2>/dev/null || true\n\n" if config.skills else "\n"
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
{skills_copy}
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

  jobs:
    {name}_keepalive:
      name: {name}-keepalive
      description: Pings /readyz every 5 minutes to prevent cold starts.
      schedule:
        quartz_cron_expression: "0 0/5 * * * ?"
        timezone_id: UTC
        pause_status: UNPAUSED
      environments:
        - environment_key: default
          spec:
            client: "1"
            dependencies:
              - requests
      tasks:
        - task_key: ping
          environment_key: default
          spark_python_task:
            python_file: ./.build/agent_server/keepalive.py
            parameters:
              - ${{resources.apps.{name}.url}}/readyz

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


def generate_project(
    config: "AgentConfig",
    target_dir: Path,
    *,
    source_dir: Path | None = None,
) -> None:
    """Write a deployable Databricks Apps project to *target_dir*.

    *target_dir* is created if it does not exist. Any files that already exist
    in the directory are overwritten.

    When ``config.skills`` is non-empty, each skill's markdown file is copied
    from *source_dir* (or ``target_dir.parent`` when *source_dir* is ``None``)
    into ``target_dir/skills/<skill.name>.md``.

    :param config: Validated ``AgentConfig`` describing the agent.
    :param target_dir: Destination directory for the generated project.
    :param source_dir: Base directory for resolving relative skill paths.
        Defaults to ``target_dir.parent`` when ``None``.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    # pyproject.toml
    (target_dir / "pyproject.toml").write_text(_build_pyproject(config))

    # agent.py — single Python definition of the agent (Python-canonical). The
    # template selects the constructor; the runtime imports this module.
    if config.template is not None:
        (target_dir / "agent.py").write_text(render_agent_py(config))
    else:
        # Template-less spec (persona/orchestrator agents): a bare LlmAgent.
        # The envelope — instructions, model, sub_agents, config tools — is
        # layered on from [tool.apx.agent] / [[tool.apx.tools]] at startup, so
        # the leaf needs no constructor args. Without this file the container
        # dies on `from agent import agent` (start_server imports it).
        (target_dir / "agent.py").write_text(
            "from apx_agent import LlmAgent\n\nagent = LlmAgent(tools=[])\n"
        )

    # agent_server/
    agent_server_dir = target_dir / "agent_server"
    agent_server_dir.mkdir(exist_ok=True)
    (agent_server_dir / "__init__.py").write_text("")
    (agent_server_dir / "start_server.py").write_text(_START_SERVER_CONTENT)
    (agent_server_dir / "keepalive.py").write_text(_KEEPALIVE_CONTENT)

    # databricks.yml
    (target_dir / "databricks.yml").write_text(_build_databricks_yml(config))

    # app.yml
    (target_dir / "app.yml").write_text(_build_app_yml(config))

    # skills/ — copy each declared skill file into the project
    if config.skills:
        _base = source_dir if source_dir is not None else target_dir.parent
        skills_dir = target_dir / "skills"
        skills_dir.mkdir(exist_ok=True)
        for skill in config.skills:
            src = Path(skill.path)
            if not src.is_absolute():
                src = _base / skill.path
            (skills_dir / f"{skill.name}.md").write_text(src.read_text())
