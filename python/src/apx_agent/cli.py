"""apx — command-line interface for the apx-agent framework.

Subcommands:

  apx scaffold <name>           Generate a new agent project
  apx run                       Run the agent locally (uvicorn against create_app)
  apx eval <evalset>            Run Mosaic AI Agent Evaluation
  apx deploy                    Log to MLflow + deploy via databricks.agents.deploy
  apx publish-tools             Publish @tool(uc=...) decorated tools to UC
  apx publish                   Register the deployed endpoint as a Supervisor sub-agent
  apx mcp-config                Emit the Managed MCP client config snippet
  apx version                   Print the package version

Every command that operates on an agent accepts ``--module MODULE:VAR``
to point at the agent definition (defaults to ``agent:agent``). The module
must be importable from the current working directory.

The CLI is a thin orchestration layer over the primitives — every command
maps to a single ``apx_agent`` call. The CLI's value isn't logic; it's
ergonomics. Implementation should stay narrow.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import logging
import sys
from pathlib import Path
from typing import Any

import click

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent loader
# ---------------------------------------------------------------------------


def _parse_module_spec(spec: str) -> tuple[str, str]:
    """Parse ``"module:variable"`` into ``(module, variable)``.

    Raises a ``click.BadParameter`` on malformed input.
    """
    if ":" not in spec:
        raise click.BadParameter(
            f"Expected MODULE:VARIABLE, got {spec!r}. "
            f"Example: 'my_agent:agent'."
        )
    module_path, _, variable = spec.partition(":")
    if not module_path or not variable:
        raise click.BadParameter(
            f"Both MODULE and VARIABLE must be non-empty, got {spec!r}."
        )
    return module_path, variable


def _read_apx_agent_config(pyproject_path: Path | None = None) -> dict[str, Any]:
    """Read ``[tool.apx.agent]`` from ``pyproject.toml`` in cwd.

    Returns an empty dict if the file is missing, malformed, or doesn't
    have the section. Used by CLI commands to read defaults like
    ``experiment`` without forcing the user to pass them on every call.
    """
    path = pyproject_path or Path.cwd() / "pyproject.toml"
    if not path.exists():
        return {}
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        data = tomllib.loads(path.read_text())
    except Exception:
        return {}
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        return {}
    apx = tool.get("apx", {})
    if not isinstance(apx, dict):
        return {}
    agent_cfg = apx.get("agent", {})
    if not isinstance(agent_cfg, dict):
        return {}
    return agent_cfg


def _load_agent(module_spec: str) -> Any:
    """Import ``module:variable`` and return the agent.

    Adds ``.`` to ``sys.path`` so the user's local module is importable
    without explicit packaging.
    """
    module_path, variable = _parse_module_spec(module_spec)
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise click.ClickException(
            f"Failed to import {module_path!r}: {e}. "
            f"Make sure the module is on PYTHONPATH or in the current directory."
        ) from e
    if not hasattr(module, variable):
        raise click.ClickException(
            f"Module {module_path!r} has no attribute {variable!r}."
        )
    return getattr(module, variable)


# ---------------------------------------------------------------------------
# CLI root
# ---------------------------------------------------------------------------


def _resolve_version() -> str:
    try:
        return importlib.metadata.version("apx-agent")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


@click.group()
@click.version_option(_resolve_version(), package_name="apx-agent", prog_name="apx")
def main() -> None:
    """apx — declarative agents on Databricks. See `apx --help` for commands."""


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@main.command()
def version() -> None:
    """Print the installed apx-agent version."""
    try:
        v = importlib.metadata.version("apx-agent")
    except importlib.metadata.PackageNotFoundError:
        v = "dev (editable install)"
    click.echo(v)


# ---------------------------------------------------------------------------
# scaffold
# ---------------------------------------------------------------------------


_SCAFFOLD_PYPROJECT = '''\
[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["apx-agent"]

[tool.apx.agent]
name = "{name}"
description = "An apx-agent."
model = "databricks-claude-sonnet-4-6"
instructions = "You are a helpful assistant."
'''


_SCAFFOLD_AGENT = '''\
"""Agent definition for {name}."""

from apx_agent import Agent, Dependencies, tool


@tool
def echo(message: str) -> str:
    """Echo a message back to the user."""
    return f"echo: {{message}}"


agent = Agent(
    instructions="You are a helpful assistant. Use the echo tool when asked.",
    tools=[echo],
)
'''


_SCAFFOLD_APP = '''\
"""FastAPI app entry point — for `apx run` / Databricks Apps hosting."""

from apx_agent import create_app

from agent import agent

app = create_app(agent)
'''


_SCAFFOLD_GITIGNORE = """\
__pycache__/
*.pyc
.venv/
.env
.env.local
mlruns/
.databricks/
"""


_SCAFFOLD_README = '''\
# {name}

An apx-agent.

## Local dev

```bash
uv sync
apx run             # uvicorn against app.py:app
```

## Deploy to Model Serving

```bash
apx publish-tools                                          # any @tool(uc=...) tools
apx deploy --name main.agents.{name}                       # logs + deploys
apx publish --endpoint {name} --supervisor SUPERVISOR_ID   # optional
```
'''


@main.command()
@click.argument("name")
@click.option(
    "--dir", "directory",
    default=".",
    type=click.Path(file_okay=False),
    help="Target directory. Default: current directory.",
)
@click.option("--force", is_flag=True, help="Overwrite existing files.")
def scaffold(name: str, directory: str, force: bool) -> None:
    """Generate a new agent project at <NAME>."""
    target = Path(directory) / name
    if target.exists() and not force:
        if any(target.iterdir()):
            raise click.ClickException(
                f"{target} already exists and is not empty. Pass --force to overwrite."
            )
    target.mkdir(parents=True, exist_ok=True)

    files = {
        "pyproject.toml": _SCAFFOLD_PYPROJECT.format(name=name),
        "agent.py": _SCAFFOLD_AGENT.format(name=name),
        "app.py": _SCAFFOLD_APP,
        ".gitignore": _SCAFFOLD_GITIGNORE,
        "README.md": _SCAFFOLD_README.format(name=name),
    }

    for rel_path, content in files.items():
        path = target / rel_path
        if path.exists() and not force:
            click.echo(f"  skip   {path} (exists; pass --force to overwrite)")
            continue
        path.write_text(content)
        click.echo(f"  write  {path}")

    click.echo()
    click.echo(f"Scaffolded {name} at {target}.")
    click.echo("Next: cd {0} && uv sync && apx run".format(name))


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--module",
    default="app:app",
    help='FastAPI app module spec, "module:variable". Default: "app:app".',
)
@click.option("--port", default=8000, type=int, help="Port. Default: 8000.")
@click.option("--host", default="127.0.0.1", help="Host. Default: 127.0.0.1.")
@click.option("--reload", is_flag=True, help="Enable auto-reload for dev.")
def run(module: str, port: int, host: str, reload: bool) -> None:
    """Run the agent locally via uvicorn against the FastAPI app."""
    try:
        import uvicorn
    except ImportError as e:
        raise click.ClickException(
            "uvicorn is required for `apx run`. Install with: "
            "pip install 'uvicorn[standard]'"
        ) from e
    # uvicorn handles the module:app parsing itself
    uvicorn.run(module, host=host, port=port, reload=reload)


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------


@main.command("eval")
@click.argument("evalset", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--module",
    default="agent:agent",
    help='Agent module spec. Default: "agent:agent".',
)
@click.option("--model", required=True, help="Databricks serving endpoint for the LLM.")
@click.option(
    "--user-token", default=None,
    help="Optional OBO user token to evaluate under a specific user identity.",
)
@click.option(
    "--experiment", default=None,
    help="MLflow experiment name/path. Falls back to [tool.apx.agent].experiment "
         "in pyproject.toml; falls back to MLflow's default when neither is set.",
)
def eval_cmd(
    evalset: str,
    module: str,
    model: str,
    user_token: str | None,
    experiment: str | None,
) -> None:
    """Run Mosaic AI Agent Evaluation against EVALSET."""
    from apx_agent import evaluate

    agent = _load_agent(module)

    # Auto-detect JSON / JSONL / YAML / CSV. mlflow.genai.evaluate accepts
    # most of these via the data kwarg, but only some via path — we read
    # JSON/JSONL ourselves for predictability.
    data: Any
    path = Path(evalset)
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        data = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    elif suffix == ".json":
        data = json.loads(path.read_text())
    else:
        # Forward path verbatim — mlflow handles CSV / Parquet / etc.
        data = evalset

    effective_experiment = experiment or _read_apx_agent_config().get("experiment")
    result = evaluate(
        agent,
        model=model,
        evalset=data,
        user_token=user_token,
        experiment=effective_experiment,
    )
    click.echo(f"Eval complete. Result: {result}")


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------


@main.command()
@click.option("--module", default="agent:agent", help='Agent module spec.')
@click.option("--model", required=True, help="Databricks serving endpoint for the LLM.")
@click.option(
    "--name", "registered_model_name", required=True,
    help="UC three-part name to register the model under (catalog.schema.model).",
)
@click.option("--no-deploy", is_flag=True,
              help="Log + register only, skip databricks.agents.deploy.")
@click.option(
    "--experiment", default=None,
    help="MLflow experiment name/path. Falls back to [tool.apx.agent].experiment "
         "in pyproject.toml; falls back to MLflow's default when neither is set.",
)
@click.option(
    "--publish-tools/--no-publish-tools", default=True,
    help="Publish @tool(uc=...) decorated tools to Unity Catalog before logging. "
         "On by default; pass --no-publish-tools to skip.",
)
@click.option(
    "--set-uc-tags/--no-set-uc-tags", default=True,
    help="Write apx.agent.* UC tags on the registered model after deploy so "
         "the agent shows up in apx list / topology / watchdog crawls. On by default.",
)
@click.option(
    "--agent-name", default=None,
    help="Friendly agent name used for the apx.agent.name UC tag. Falls back to "
         "[tool.apx.agent].name in pyproject.toml; falls back to the short part of --name.",
)
def deploy(
    module: str,
    model: str,
    registered_model_name: str,
    no_deploy: bool,
    experiment: str | None,
    publish_tools: bool,
    set_uc_tags: bool,
    agent_name: str | None,
) -> None:
    """Log the agent to MLflow + deploy + UC-tag in one command.

    By default runs the full canonical flow:

      1. publish_tools_to_uc(agent)    — register any @tool(uc=...) tools
      2. log_agent(agent, ...)         — log to MLflow + register in UC
      3. databricks.agents.deploy(...) — promote to a serving endpoint
      4. set_uc_tags_for_agent(...)    — write apx.agent.* tags

    Toggle individual stages with --no-publish-tools, --no-deploy, or
    --no-set-uc-tags.
    """
    import mlflow

    from apx_agent import log_agent

    agent = _load_agent(module)
    config = _read_apx_agent_config()
    effective_experiment = experiment or config.get("experiment")
    effective_agent_name = (
        agent_name
        or config.get("name")
        or registered_model_name.rsplit(".", 1)[-1]
    )
    if effective_experiment:
        click.echo(f"# experiment: {effective_experiment}", err=True)

    # 1. Publish @tool(uc=...) tools first so they exist in UC by the
    # time log_agent's resource collector picks them up.
    if publish_tools:
        try:
            from apx_agent import publish_tools_to_uc
            results = publish_tools_to_uc(agent)
            if results:
                for r in results:
                    grants = ", ".join(r.grants_applied) or "none"
                    click.echo(f"  published {r.uc_name} (grants: {grants})")
            else:
                click.echo("  (no @tool(uc=...) decorated tools to publish)")
        except Exception as e:
            click.echo(f"# publish-tools failed: {e}", err=True)
            click.echo("# continuing with log + deploy", err=True)

    # 2. Log + register
    with mlflow.start_run():
        info = log_agent(
            agent,
            model=model,
            registered_model_name=registered_model_name,
            experiment=effective_experiment,
        )
    click.echo(f"Logged {registered_model_name} version {info.registered_model_version}")

    # 3. Deploy to Model Serving
    if not no_deploy:
        try:
            from databricks import agents  # type: ignore[attr-defined]
        except ImportError as e:
            raise click.ClickException(
                "databricks-agents is required for deployment. "
                "Install with: pip install databricks-agents"
            ) from e
        agents.deploy(registered_model_name, model_version=info.registered_model_version)
        click.echo(
            f"Deployed {registered_model_name} version "
            f"{info.registered_model_version} as a serving endpoint."
        )
    else:
        click.echo("Skipping deploy (--no-deploy).")

    # 4. Set UC tags so the agent shows up in apx list / topology / watchdog
    if set_uc_tags:
        try:
            from apx_agent import set_uc_tags_for_agent
            set_uc_tags_for_agent(
                agent,
                registered_model_name=registered_model_name,
                model=model,
                name=effective_agent_name,
            )
            click.echo(f"  apx.agent.* tags written on {registered_model_name} "
                       f"(agent_name={effective_agent_name})")
        except Exception as e:
            click.echo(f"# set-uc-tags failed: {e}", err=True)


# ---------------------------------------------------------------------------
# publish-tools
# ---------------------------------------------------------------------------


@main.command("publish-tools")
@click.option("--module", default="agent:agent", help='Agent module spec.')
@click.option("--dry-run", is_flag=True, help="Report what would publish without writing.")
def publish_tools_cmd(module: str, dry_run: bool) -> None:
    """Publish all @tool(uc=...) decorated tools to Unity Catalog."""
    from apx_agent import publish_tools_to_uc

    agent = _load_agent(module)
    results = publish_tools_to_uc(agent, dry_run=dry_run)
    if not results:
        click.echo("No @tool(uc=...) decorated tools found.")
        return
    for r in results:
        prefix = "DRY-RUN" if r.skipped else "PUBLISHED"
        grants = ", ".join(r.grants_applied) if r.grants_applied else "none"
        click.echo(f"  {prefix}  {r.uc_name}  (grants: {grants})")


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


@main.command()
@click.option("--endpoint", required=True, help="Serving endpoint name of the deployed sub-agent.")
@click.option("--supervisor", "supervisor_id", required=True, help="Supervisor Agent ID.")
@click.option("--description", required=True, help="When the supervisor should route to this sub-agent.")
@click.option("--display-name", default=None, help="Optional human-readable name.")
@click.option("--tool-id", default=None, help="Optional stable tool_id (idempotent re-publish).")
def publish(
    endpoint: str,
    supervisor_id: str,
    description: str,
    display_name: str | None,
    tool_id: str | None,
) -> None:
    """Register a deployed serving endpoint as a Supervisor sub-agent."""
    from apx_agent import publish_to_supervisor

    result = publish_to_supervisor(
        supervisor_agent_id=supervisor_id,
        serving_endpoint=endpoint,
        description=description,
        display_name=display_name,
        tool_id=tool_id,
    )
    click.echo(f"Registered {endpoint} as sub-agent on supervisor {supervisor_id}.")
    click.echo(f"Result: {result}")


# ---------------------------------------------------------------------------
# mcp-config
# ---------------------------------------------------------------------------


@main.command("mcp-config")
@click.option("--module", default="agent:agent", help='Agent module spec.')
@click.option("--host", "workspace_host", required=True, help="Databricks workspace host.")
@click.option("--name", default="databricks", help="Prefix for the mcpServers entries.")
@click.option(
    "--include-unsupported", is_flag=True,
    help="Include resources that don't map to a Managed MCP endpoint (serving endpoints, warehouses).",
)
def mcp_config(
    module: str,
    workspace_host: str,
    name: str,
    include_unsupported: bool,
) -> None:
    """Emit the Managed MCP client config snippet for the agent's resources."""
    from apx_agent import managed_mcp_client_config, managed_mcp_urls

    agent = _load_agent(module)
    endpoints = managed_mcp_urls(agent, workspace_host=workspace_host)
    config = managed_mcp_client_config(
        endpoints, name=name, include_unsupported=include_unsupported,
    )
    click.echo(json.dumps(config, indent=2))


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


def _resolve_served_model_name(ws: Any, endpoint_name: str) -> str:
    """Find the most recent served-model name on a serving endpoint.

    Walks ``ws.serving_endpoints.get(name).config.served_models`` and
    returns the first entry's name. Raises ``click.ClickException`` with
    a friendly message if the endpoint has no served models yet.
    """
    endpoint = ws.serving_endpoints.get(endpoint_name)
    config = getattr(endpoint, "config", None) or getattr(endpoint, "pending_config", None)
    if config is None:
        raise click.ClickException(
            f"Endpoint {endpoint_name!r} has no config — has it finished deploying?"
        )
    served_models = (
        getattr(config, "served_entities", None)
        or getattr(config, "served_models", None)
        or []
    )
    if not served_models:
        raise click.ClickException(
            f"Endpoint {endpoint_name!r} has no served models on its config."
        )
    name = getattr(served_models[0], "name", None)
    if not name:
        raise click.ClickException(
            f"Endpoint {endpoint_name!r}: first served model has no name field."
        )
    return name


@main.command()
@click.option("--endpoint", default=None, help="Model Serving endpoint name.")
@click.option("--served-model", default=None,
              help="Specific served model on the endpoint. Auto-discovered when omitted.")
@click.option("--build", "build", is_flag=True,
              help="Fetch build logs instead of runtime/service logs.")
@click.option("--app", "app_name", default=None,
              help="Databricks Apps name (alternative to --endpoint). Uses the databricks CLI.")
@click.option("--profile", default=None,
              help="Databricks CLI profile (only used with --app).")
def logs(
    endpoint: str | None,
    served_model: str | None,
    build: bool,
    app_name: str | None,
    profile: str | None,
) -> None:
    """Fetch logs from a deployed agent.

    Two modes:

    \b
      apx logs --endpoint NAME              Runtime logs from a Model Serving endpoint
      apx logs --endpoint NAME --build      Build-time logs from the endpoint's build
      apx logs --app NAME [--profile P]     Logs from an apx-agent hosted as a Databricks App

    For the --endpoint path, ``served_model`` is auto-discovered from the
    endpoint's current config when not supplied explicitly.
    """
    if not endpoint and not app_name:
        raise click.UsageError("Pass either --endpoint NAME or --app NAME.")
    if endpoint and app_name:
        raise click.UsageError("--endpoint and --app are mutually exclusive.")

    if app_name:
        # Apps logs aren't on the Python SDK; shell out to the databricks CLI.
        import subprocess
        cmd = ["databricks", "apps", "logs", app_name]
        if profile:
            cmd.extend(["--profile", profile])
        try:
            result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        except FileNotFoundError as e:
            raise click.ClickException(
                "The 'databricks' CLI is required for --app logs. "
                "Install: https://docs.databricks.com/dev-tools/cli/install"
            ) from e
        if result.returncode != 0:
            raise click.ClickException(
                f"databricks apps logs failed (exit {result.returncode}):\n{result.stderr.strip()}"
            )
        click.echo(result.stdout)
        return

    # Endpoint path.
    from databricks.sdk import WorkspaceClient
    ws = WorkspaceClient()

    if served_model is None:
        assert endpoint is not None
        served_model = _resolve_served_model_name(ws, endpoint)
        click.echo(f"# served_model auto-discovered: {served_model}", err=True)

    method = ws.serving_endpoints.build_logs if build else ws.serving_endpoints.logs
    label = "build" if build else "runtime"
    try:
        response = method(name=endpoint, served_model_name=served_model)
    except Exception as e:
        raise click.ClickException(
            f"Failed to fetch {label} logs for {endpoint}/{served_model}: {e}"
        ) from e
    body = getattr(response, "logs", None) or str(response)
    click.echo(body)


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


@main.command()
@click.option("--module", default="agent:agent", help='Agent module spec.')
@click.option(
    "--format", "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
def info(module: str, fmt: str) -> None:
    """Introspect an agent — tools, resources, sub-agents, instructions.

    Pure local — no Databricks calls. Useful as a sanity check before
    deploy and as a programmatic source of truth for what an agent
    declares.
    """
    from apx_agent._resources import (
        _iter_sub_agents,
        _iter_tool_fns,
        collect_resource_specs,
        get_resources,
    )
    from apx_agent._tool import get_tool_metadata

    agent = _load_agent(module)

    # Walk the whole tree — HandoffAgent / SequentialAgent / etc. have no
    # _tool_fns of their own; the tools live on nested LlmAgents.
    tool_fns = list(_iter_tool_fns(agent))
    sub_agents = list(_iter_sub_agents(agent))
    instructions = getattr(agent, "_instructions", None) or ""

    tools_info: list[dict[str, Any]] = []
    for fn in tool_fns:
        meta = get_tool_metadata(fn)
        tools_info.append({
            "name": fn.__name__,
            "doc": (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else "",
            "uc_name": meta.uc_name if meta else None,
            "grants": list(meta.grants) if meta else [],
            "resources": [
                {"kind": s.kind, "identifier": s.identifier}
                for s in get_resources(fn)
            ],
        })

    resource_specs = collect_resource_specs(agent)
    resources_info = [
        {"kind": s.kind, "identifier": s.identifier}
        for s in resource_specs
    ]

    payload = {
        "module": module,
        "instructions": instructions,
        "tools": tools_info,
        "sub_agents": sub_agents,
        "resources": resources_info,
    }

    if fmt == "json":
        click.echo(json.dumps(payload, indent=2))
        return

    # text format
    click.echo(f"Agent loaded from {module}")
    if instructions:
        click.echo("\nInstructions:")
        click.echo(f"  {instructions}")
    click.echo(f"\nTools ({len(tools_info)}):")
    for t in tools_info:
        line = f"  - {t['name']}"
        if t["uc_name"]:
            line += f"  →  UC: {t['uc_name']}"
            if t["grants"]:
                line += f"  (grants: {', '.join(t['grants'])})"
        click.echo(line)
        if t["doc"]:
            click.echo(f"      {t['doc']}")
    if sub_agents:
        click.echo(f"\nSub-agents ({len(sub_agents)}):")
        for s in sub_agents:
            click.echo(f"  - {s}")
    click.echo(f"\nDeclared resources ({len(resources_info)}):")
    for r in resources_info:
        click.echo(f"  - {r['kind']:<24} {r['identifier']}")


# ---------------------------------------------------------------------------
# trace — inspect MLflow traces
# ---------------------------------------------------------------------------


@main.command()
@click.option("--experiment", default=None,
              help="MLflow experiment name/id. Falls back to "
                   "[tool.apx.agent].experiment in pyproject.toml.")
@click.option("--agent", "agent_name", default=None,
              help="Filter to traces where apx.agent.name matches.")
@click.option("--operation", default=None,
              help="Filter by apx.operation (predict, tool_call, model_call, etc.).")
@click.option("--limit", default=20, type=int, help="Max traces to return.")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def trace(
    experiment: str | None,
    agent_name: str | None,
    operation: str | None,
    limit: int,
    fmt: str,
) -> None:
    """Fetch recent MLflow traces for a deployed agent."""
    try:
        import mlflow
    except ImportError as e:
        raise click.ClickException(
            "apx trace requires mlflow. Install with: pip install 'apx-agent[eval]'"
        ) from e

    effective_experiment = experiment or _read_apx_agent_config().get("experiment")
    if not effective_experiment:
        raise click.UsageError(
            "Pass --experiment NAME or set [tool.apx.agent].experiment in pyproject.toml."
        )

    filter_parts: list[str] = []
    if agent_name:
        filter_parts.append(f"attributes.`apx.agent.name` = '{agent_name}'")
    if operation:
        filter_parts.append(f"attributes.`apx.operation` = '{operation}'")
    filter_string = " AND ".join(filter_parts) if filter_parts else None

    try:
        traces = mlflow.search_traces(  # type: ignore[attr-defined]
            experiment_names=[effective_experiment],
            filter_string=filter_string,
            max_results=limit,
        )
    except Exception as e:
        raise click.ClickException(f"mlflow.search_traces failed: {e}") from e

    rows = _normalise_trace_rows(traces)

    if fmt == "json":
        click.echo(json.dumps(rows, indent=2, default=str))
        return

    if not rows:
        click.echo("No traces matched.")
        return
    click.echo(f"{'TRACE_ID':<36}  {'AGENT':<20}  {'OPERATION':<14}  {'STATUS':<8}  {'DURATION_MS':>10}")
    for r in rows:
        click.echo(
            f"{r.get('trace_id', ''):<36}  "
            f"{(r.get('agent_name') or '-'):<20}  "
            f"{(r.get('operation') or '-'):<14}  "
            f"{(r.get('status') or '-'):<8}  "
            f"{(r.get('duration_ms') or 0):>10}"
        )


def _normalise_trace_rows(traces: Any) -> list[dict[str, Any]]:
    """Convert mlflow.search_traces output to a uniform list of dicts.

    mlflow returns either a list of Trace objects or a pandas DataFrame
    depending on the version + return_type kwarg. This helper accepts
    either and produces a flat list of dicts with the keys the CLI prints.
    """
    rows: list[dict[str, Any]] = []
    # DataFrame case
    if hasattr(traces, "to_dict"):
        try:
            records = traces.to_dict(orient="records")  # type: ignore[union-attr]
        except Exception:
            records = []
        for rec in records:
            attrs = rec.get("tags") or rec.get("attributes") or {}
            rows.append({
                "trace_id": rec.get("trace_id") or rec.get("request_id"),
                "agent_name": attrs.get("apx.agent.name"),
                "operation": attrs.get("apx.operation"),
                "status": rec.get("status"),
                "duration_ms": rec.get("execution_time_ms"),
            })
        return rows
    # List-of-Trace case
    for t in traces or []:
        info = getattr(t, "info", None)
        data = getattr(t, "data", None)
        attrs = (getattr(data, "spans", None) or [])
        # Pull root-span attributes if available; otherwise fall back to
        # trace-level tags.
        root_attrs: dict[str, Any] = {}
        if attrs:
            root = attrs[0]
            root_attrs = dict(getattr(root, "attributes", {}) or {})
        tags = dict(getattr(info, "tags", {}) or {}) if info else {}
        rows.append({
            "trace_id": getattr(info, "trace_id", None) or getattr(info, "request_id", None),
            "agent_name": root_attrs.get("apx.agent.name") or tags.get("apx.agent.name"),
            "operation": root_attrs.get("apx.operation") or tags.get("apx.operation"),
            "status": getattr(info, "status", None),
            "duration_ms": getattr(info, "execution_time_ms", None),
        })
    return rows


# ---------------------------------------------------------------------------
# lint — static checks
# ---------------------------------------------------------------------------


@main.command("lint")
@click.option("--module", default="agent:agent", help="Agent module spec.")
@click.option("--model", default=None,
              help="Model endpoint to lint (in addition to any compiled into the tree).")
@click.option("--format", "fmt",
              type=click.Choice(["text", "json"]),
              default="text",
              help="Output format.")
def lint_cmd(module: str, model: str | None, fmt: str) -> None:
    """Run static checks against an agent — instructions, tool docstrings,
    sub-agent URL env vars, model name shape.

    Exits non-zero if any ERROR findings are reported. WARNING findings
    are reported but don't fail. Pair with ``apx test`` (smoke) and
    ``apx eval`` (behavior) for a full pre-deploy check.
    """
    from ._lint import Severity, lint_agent

    agent = _load_agent(module)

    effective_model = model or _read_apx_agent_config().get("model")

    findings = lint_agent(agent, model=effective_model)

    if fmt == "json":
        click.echo(json.dumps(
            [
                {
                    "code": f.code,
                    "severity": f.severity.value,
                    "location": f.location,
                    "message": f.message,
                }
                for f in findings
            ],
            indent=2,
        ))
    else:
        if not findings:
            click.echo("apx lint: clean — no findings.")
        else:
            for f in findings:
                marker = {
                    Severity.ERROR: "ERROR",
                    Severity.WARNING: "warn ",
                    Severity.INFO: "info ",
                }[f.severity]
                click.echo(f"  [{marker}] {f.code}  {f.location}")
                click.echo(f"           {f.message}")

    n_errors = sum(1 for f in findings if f.is_error())
    if n_errors:
        click.echo(f"\napx lint: {n_errors} error(s).", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# hot-swap — change LLM endpoint on a deployed agent without re-logging
# ---------------------------------------------------------------------------


@main.command("hot-swap")
@click.option("--endpoint", required=True,
              help="Serving endpoint hosting the agent.")
@click.option("--model", required=True,
              help="New model serving endpoint (e.g. databricks-claude-opus-4-7).")
@click.option("--no-wait", is_flag=True,
              help="Don't block until the config update completes.")
def hot_swap_cmd(endpoint: str, model: str, no_wait: bool) -> None:
    """Hot-swap a deployed agent's LLM endpoint.

    Updates the APX_AGENT_MODEL_OVERRIDE env var on the serving endpoint
    so the next replica picks up the new model. The agent artifact is
    NOT re-logged — same model version, different LLM.

    Use cases: try a more capable model in production without redeploy,
    roll back a problematic model change in seconds, run experiments
    without versioning the artifact each time.

    For full artifact-version A/B with traffic split, use `apx canary`
    instead (different concern: that one creates new served entities;
    this one rewrites env vars on the existing one).
    """
    from ._hot_swap import hot_swap_model

    try:
        result = hot_swap_model(endpoint, model, wait=not no_wait)
    except Exception as e:
        click.echo(f"hot-swap failed: {type(e).__name__}: {e}", err=True)
        sys.exit(1)

    click.echo(f"apx hot-swap: {result.endpoint_name}")
    click.echo(f"  new model:      {result.new_model}")
    if result.previous_model:
        click.echo(f"  previous override: {result.previous_model}")
    else:
        click.echo(f"  previous override: (none — first swap on this endpoint)")
    click.echo(f"  served entities updated: {result.served_entities_updated}")
    if no_wait:
        click.echo("  (update dispatched async; pass --wait or check `databricks serving-endpoints get` to confirm)")


# ---------------------------------------------------------------------------
# test — local smoke test
# ---------------------------------------------------------------------------


@main.command("test")
@click.option("--module", default="agent:agent", help="Agent module spec.")
@click.option("--prompt", "prompts", multiple=True,
              help='Prompt to send. Repeat for multiple; defaults to one "hi" prompt.')
@click.option("--prompts-file", "prompts_file", default=None,
              type=click.Path(exists=True, dir_okay=False),
              help="Newline-separated file of prompts (one per line).")
@click.option("--model", default=None,
              help="LLM endpoint. Defaults to [tool.apx.agent].model in pyproject.toml.")
def test_cmd(
    module: str,
    prompts: tuple[str, ...],
    prompts_file: str | None,
    model: str | None,
) -> None:
    """Compile the agent locally and send sample prompts through it.

    Smoke test for "did I break the import / can the agent at least
    accept a message and return something?" — cheaper than apx eval,
    no MLflow / eval dataset required.
    """
    agent = _load_agent(module)

    effective_model = model or _read_apx_agent_config().get("model")
    if not effective_model:
        raise click.UsageError(
            "Pass --model NAME or set [tool.apx.agent].model in pyproject.toml."
        )

    prompt_list: list[str] = list(prompts)
    if prompts_file:
        with open(prompts_file) as f:
            prompt_list.extend(line.strip() for line in f if line.strip())
    if not prompt_list:
        prompt_list = ["hi"]

    try:
        from apx_agent import compile_to_chat_agent
        from mlflow.types.agent import ChatAgentMessage
    except ImportError as e:
        raise click.ClickException(
            "apx test requires the eval + langgraph extras. "
            "Install with: pip install 'apx-agent[eval,langgraph]'"
        ) from e

    chat_agent = compile_to_chat_agent(agent, model=effective_model)

    import time
    failures = 0
    for i, prompt in enumerate(prompt_list, start=1):
        click.echo(f"\n--- prompt {i}: {prompt!r}")
        start = time.time()
        try:
            response = chat_agent.predict(
                messages=[ChatAgentMessage(role="user", content=prompt)],
            )
            elapsed_ms = int((time.time() - start) * 1000)
            messages = getattr(response, "messages", []) or []
            assistant = next(
                (m for m in reversed(messages)
                 if getattr(m, "role", None) == "assistant"),
                None,
            )
            text = getattr(assistant, "content", "") if assistant else ""
            preview = (text or "").strip().splitlines()[0] if text else "(empty)"
            click.echo(f"    ok  ({elapsed_ms} ms)  {preview[:120]}")
        except Exception as e:
            failures += 1
            elapsed_ms = int((time.time() - start) * 1000)
            click.echo(f"    FAIL ({elapsed_ms} ms): {type(e).__name__}: {e}", err=True)

    click.echo(
        f"\n{len(prompt_list) - failures}/{len(prompt_list)} prompts passed.",
        err=True,
    )
    if failures:
        raise click.exceptions.Exit(1)


# ---------------------------------------------------------------------------
# list — discover deployed apx-agents
# ---------------------------------------------------------------------------


@main.command("list")
@click.option("--catalog", default=None,
              help="Restrict to a UC catalog. Default: any.")
@click.option("--schema", default=None,
              help="Restrict to a UC schema (requires --catalog).")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def list_cmd(catalog: str | None, schema: str | None, fmt: str) -> None:
    """Discover apx-agents in the workspace by their UC tags.

    Looks for registered models tagged ``apx.agent.name`` — the tag
    ``set_uc_tags_for_agent`` writes after deploy. Prints (name, model,
    endpoint hint, resource count). Useful for fleet operators
    inventorying multi-agent workspaces.
    """
    if schema and not catalog:
        raise click.UsageError("--schema requires --catalog.")

    try:
        from databricks.sdk import WorkspaceClient
    except ImportError as e:
        raise click.ClickException(
            "apx list requires databricks-sdk."
        ) from e

    ws = WorkspaceClient()

    filter_parts: list[str] = []
    if catalog:
        filter_parts.append(f"catalog_name = '{catalog}'")
    if schema:
        filter_parts.append(f"schema_name = '{schema}'")
    filter_string = " AND ".join(filter_parts) if filter_parts else None

    try:
        models_iter = ws.registered_models.list(
            catalog_name=catalog,
            schema_name=schema,
            include_browse=False,
        )
        models = list(models_iter)
    except TypeError:
        # Older SDK signatures took different kwargs; fall back to a no-filter list.
        models = list(ws.registered_models.list())  # type: ignore[call-arg]

    rows: list[dict[str, Any]] = []
    for m in models:
        tags = {t.key: t.value for t in (getattr(m, "tags", None) or [])}
        if "apx.agent.name" not in tags:
            continue
        # Resource count parsed off the metadata blob when present
        resource_count = 0
        try:
            metadata_json = tags.get("apx.agent.metadata") or "{}"
            parsed = json.loads(metadata_json)
            resource_count = len(parsed.get("resources") or [])
        except Exception:
            pass
        rows.append({
            "agent_name": tags.get("apx.agent.name"),
            "model_endpoint": tags.get("apx.agent.model"),
            "uc_name": getattr(m, "full_name", None) or f"{getattr(m, 'catalog_name','')}.{getattr(m, 'schema_name','')}.{getattr(m, 'name','')}",
            "tool_count": tags.get("apx.agent.tool_count"),
            "resource_count": resource_count,
        })

    if fmt == "json":
        click.echo(json.dumps(rows, indent=2, default=str))
        return

    if not rows:
        click.echo("No apx-tagged registered models found.")
        return
    click.echo(f"{'AGENT':<28}  {'UC NAME':<40}  {'MODEL':<28}  {'TOOLS':>6}  {'RESOURCES':>9}")
    for r in rows:
        click.echo(
            f"{(r['agent_name'] or '-'):<28}  "
            f"{(r['uc_name'] or '-'):<40}  "
            f"{(r['model_endpoint'] or '-'):<28}  "
            f"{(r['tool_count'] or '-'):>6}  "
            f"{r['resource_count']:>9}"
        )


# ---------------------------------------------------------------------------
# cost — DBU / $ per agent or endpoint over a lookback window
# ---------------------------------------------------------------------------


@main.command()
@click.option("--agent", "agent_name", default=None,
              help="Agent name. Resolves to the serving endpoint of the same name.")
@click.option("--endpoint", default=None,
              help="Serving endpoint name. Use this when the endpoint name differs from the agent name.")
@click.option("--hours", default=24, type=int,
              help="Lookback window in hours. Default 24.")
@click.option("--warehouse-id", default=None,
              help="SQL warehouse to run the system-tables query on.")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def cost(
    agent_name: str | None,
    endpoint: str | None,
    hours: int,
    warehouse_id: str | None,
    fmt: str,
) -> None:
    """Report DBU + $ for an agent or serving endpoint over the lookback window.

    Queries ``system.billing.usage`` joined to
    ``system.billing.list_prices`` (best-effort) scoped to the serving
    endpoint name. Requires the system billing share to be enabled in
    the workspace.
    """
    if not agent_name and not endpoint:
        raise click.UsageError("Pass --agent NAME or --endpoint NAME.")
    if agent_name and endpoint:
        raise click.UsageError("--agent and --endpoint are mutually exclusive.")

    try:
        from databricks.sdk import WorkspaceClient
    except ImportError as e:
        raise click.ClickException("apx cost requires databricks-sdk.") from e

    from apx_agent import cost_for_agent

    ws = WorkspaceClient()
    breakdown = cost_for_agent(
        agent_name=agent_name,
        endpoint=endpoint,
        ws=ws,
        lookback_hours=hours,
        warehouse_id=warehouse_id,
    )

    if fmt == "json":
        click.echo(json.dumps({
            "endpoint": breakdown.endpoint,
            "lookback_hours": breakdown.lookback_hours,
            "total_dbus": breakdown.total_dbus,
            "total_usd": breakdown.total_usd,
            "rows": breakdown.rows,
        }, indent=2, default=str))
        return

    click.echo(f"# cost for {breakdown.endpoint} (last {breakdown.lookback_hours}h)")
    if not breakdown.rows:
        click.echo("No usage rows. Either no traffic in the window or "
                   "system.billing.usage isn't enabled in this workspace.")
        return
    click.echo(f"{'SKU':<42}  {'UNIT':<10}  {'DBUs':>12}  {'USD':>10}")
    for r in breakdown.rows:
        usd = r.get("usd")
        usd_str = f"${usd:,.2f}" if isinstance(usd, (int, float)) else "-"
        click.echo(
            f"{(r.get('sku_name') or '-'):<42}  "
            f"{(r.get('usage_unit') or '-'):<10}  "
            f"{r.get('dbus') or 0:>12,.2f}  "
            f"{usd_str:>10}"
        )
    click.echo(f"\nTotal DBUs: {breakdown.total_dbus:,.2f}")
    if breakdown.total_usd is not None:
        click.echo(f"Total USD:  ${breakdown.total_usd:,.2f}")
    else:
        click.echo("Total USD:  - (pricing data not joinable)")


# ---------------------------------------------------------------------------
# export-traces
# ---------------------------------------------------------------------------


@main.command("export-traces")
@click.option("--experiment", default=None,
              help="MLflow experiment. Falls back to [tool.apx.agent].experiment.")
@click.option("--table", "target_table", required=True,
              help="Three-part UC name (catalog.schema.table) for the destination Delta table.")
@click.option("--hours", default=24, type=int, help="Lookback window in hours.")
@click.option("--warehouse-id", default=None, help="SQL warehouse for the Delta writes.")
@click.option("--max-traces", default=1000, type=int, help="Max traces per export run.")
def export_traces_cmd(
    experiment: str | None,
    target_table: str,
    hours: int,
    warehouse_id: str | None,
    max_traces: int,
) -> None:
    """Export MLflow traces to a Delta table for analytics."""
    effective_experiment = experiment or _read_apx_agent_config().get("experiment")
    if not effective_experiment:
        raise click.UsageError(
            "Pass --experiment NAME or set [tool.apx.agent].experiment in pyproject.toml."
        )

    try:
        from databricks.sdk import WorkspaceClient
    except ImportError as e:
        raise click.ClickException("export-traces requires databricks-sdk.") from e

    from apx_agent import export_traces

    ws = WorkspaceClient()
    result = export_traces(
        experiment_name=effective_experiment,
        target_table=target_table,
        ws=ws,
        lookback_hours=hours,
        warehouse_id=warehouse_id,
        max_traces=max_traces,
    )
    click.echo(
        f"Exported {result.rows_written}/{result.traces_pulled} traces "
        f"to {result.target_table} (skipped {result.skipped})."
    )


# ---------------------------------------------------------------------------
# topology
# ---------------------------------------------------------------------------


@main.command()
@click.option("--catalog", default=None, help="Restrict to a UC catalog.")
@click.option("--schema", default=None, help="Restrict to a UC schema (requires --catalog).")
@click.option(
    "--format", "fmt", type=click.Choice(["mermaid", "graphviz"]),
    default="mermaid", help="Output format. Default: mermaid.",
)
@click.option("--output", "output_file", default=None,
              type=click.Path(dir_okay=False),
              help="Write the rendered diagram to FILE instead of stdout.")
def topology(
    catalog: str | None,
    schema: str | None,
    fmt: str,
    output_file: str | None,
) -> None:
    """Render the multi-agent endpoint graph from UC tags."""
    if schema and not catalog:
        raise click.UsageError("--schema requires --catalog.")

    try:
        from databricks.sdk import WorkspaceClient
    except ImportError as e:
        raise click.ClickException("apx topology requires databricks-sdk.") from e

    from apx_agent import discover_topology, render_topology

    ws = WorkspaceClient()
    topo = discover_topology(ws, catalog=catalog, schema=schema)
    text = render_topology(topo, format=fmt)

    if output_file:
        with open(output_file, "w") as f:
            f.write(text + "\n")
        click.echo(f"Wrote {len(topo.nodes)} nodes, {len(topo.edges)} edges to {output_file}",
                   err=True)
    else:
        click.echo(text)


# ---------------------------------------------------------------------------
# eval-chain
# ---------------------------------------------------------------------------


@main.command("eval-chain")
@click.argument("evalset", type=click.Path(exists=True, dir_okay=False))
@click.option("--module", default="agent:agent", help="Agent module spec.")
@click.option("--model", required=True, help="LLM endpoint for the supervisor.")
@click.option(
    "--experiment", required=True,
    help="MLflow experiment to log into + read traces from for chain correlation.",
)
@click.option("--user-token", default=None,
              help="Optional OBO token for user-scoped eval.")
def eval_chain_cmd(
    evalset: str,
    module: str,
    model: str,
    experiment: str,
    user_token: str | None,
) -> None:
    """Eval a multi-agent chain — per-prompt + per-sub-agent coverage."""
    agent = _load_agent(module)

    # Load the evalset same way apx eval does
    data: Any
    path = Path(evalset)
    if path.suffix.lower() == ".jsonl":
        data = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    elif path.suffix.lower() == ".json":
        data = json.loads(path.read_text())
    else:
        data = evalset

    from apx_agent import evaluate_chain

    report = evaluate_chain(
        agent,
        model=model,
        evalset=data,
        experiment=experiment,
        user_token=user_token,
    )

    click.echo(f"# chain-eval cases: {len(report.cases)}")
    for case in report.cases:
        subs = ", ".join(case.sub_agents_invoked) if case.sub_agents_invoked else "-"
        tools = ", ".join(case.tool_calls) if case.tool_calls else "-"
        click.echo(f"\n  request: {case.request!r}")
        click.echo(f"  duration_ms: {case.duration_ms or '-'}")
        click.echo(f"  sub_agents: {subs}")
        click.echo(f"  tools:      {tools}")
    if report.sub_agent_coverage:
        click.echo("\n# sub-agent coverage")
        for sub, count in sorted(
            report.sub_agent_coverage.items(), key=lambda kv: -kv[1],
        ):
            click.echo(f"  {sub}: {count}")


# ---------------------------------------------------------------------------
# canary — multi-version traffic split helpers
# ---------------------------------------------------------------------------


@main.group()
def canary() -> None:
    """Canary / A-B deployment helpers — multi-version traffic split."""


@canary.command("status")
@click.option("--endpoint", required=True, help="Model Serving endpoint name.")
def canary_status(endpoint: str) -> None:
    """Print the endpoint's current served entities + traffic split."""
    from databricks.sdk import WorkspaceClient

    from apx_agent import get_canary_config

    cfg = get_canary_config(endpoint, ws=WorkspaceClient())
    click.echo(f"# canary status: {cfg.endpoint}")
    if not cfg.served_entities:
        click.echo("  (no served entities)")
        return
    click.echo(f"{'ENTITY':<40}  {'MODEL':<32}  {'VERSION':<10}  {'TRAFFIC %':>9}")
    for name, entity, version in cfg.served_entities:
        pct = cfg.traffic_split.get(name, 0)
        click.echo(f"{name:<40}  {entity:<32}  {version:<10}  {pct:>9}")


@canary.command("deploy")
@click.option("--endpoint", required=True, help="Model Serving endpoint to add the canary to.")
@click.option("--model", "registered_model_name", required=True,
              help="Three-part UC name of the registered model.")
@click.option("--version", required=True, help="Model version to canary.")
@click.option("--traffic", "traffic_pct", default=10, type=int,
              help="Percentage of traffic to route to the new version. Default 10.")
@click.option("--workload-size", default="Small", help="Workload size for the new served entity.")
@click.option("--no-scale-to-zero", is_flag=True,
              help="Disable scale-to-zero on the new served entity.")
def canary_deploy(
    endpoint: str,
    registered_model_name: str,
    version: str,
    traffic_pct: int,
    workload_size: str,
    no_scale_to_zero: bool,
) -> None:
    """Add a new model version as a canary served entity."""
    from databricks.sdk import WorkspaceClient

    from apx_agent import deploy_canary

    cfg = deploy_canary(
        endpoint=endpoint,
        registered_model_name=registered_model_name,
        new_version=version,
        canary_traffic_pct=traffic_pct,
        ws=WorkspaceClient(),
        scale_to_zero_enabled=not no_scale_to_zero,
        workload_size=workload_size,
    )
    click.echo(f"Deployed {registered_model_name} v{version} at {traffic_pct}% on {endpoint}.")
    click.echo(f"New split: {cfg.traffic_split}")


@canary.command("promote")
@click.option("--endpoint", required=True)
@click.option("--model", "registered_model_name", required=True,
              help="Three-part UC name of the registered model.")
@click.option("--version", required=True, help="Version to send 100% of traffic to.")
def canary_promote(
    endpoint: str, registered_model_name: str, version: str,
) -> None:
    """Send 100% of traffic to a version. Other entities stay configured."""
    from databricks.sdk import WorkspaceClient

    from apx_agent import promote_canary

    cfg = promote_canary(
        endpoint=endpoint,
        registered_model_name=registered_model_name,
        version=version,
        ws=WorkspaceClient(),
    )
    click.echo(f"Promoted {registered_model_name} v{version} to 100% on {endpoint}.")
    click.echo(f"Split: {cfg.traffic_split}")


@canary.command("rollback")
@click.option("--endpoint", required=True)
@click.option("--model", "registered_model_name", required=True,
              help="Three-part UC name of the registered model.")
@click.option("--version", required=True, help="Version to roll back to (usually the prior production version).")
def canary_rollback(
    endpoint: str, registered_model_name: str, version: str,
) -> None:
    """Roll back to a prior version. Functionally equivalent to promote."""
    from databricks.sdk import WorkspaceClient

    from apx_agent import rollback_canary

    cfg = rollback_canary(
        endpoint=endpoint,
        registered_model_name=registered_model_name,
        version=version,
        ws=WorkspaceClient(),
    )
    click.echo(f"Rolled back to {registered_model_name} v{version} on {endpoint}.")
    click.echo(f"Split: {cfg.traffic_split}")


@canary.command("analyze")
@click.option("--endpoint", required=True)
@click.option("--experiment", default=None,
              help="MLflow experiment to read traces from. Falls back to "
                   "[tool.apx.agent].experiment in pyproject.toml.")
@click.option("--hours", default=24, type=int, help="Lookback window. Default 24h.")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def canary_analyze(
    endpoint: str,
    experiment: str | None,
    hours: int,
    fmt: str,
) -> None:
    """Per-version requests / errors / latency from MLflow traces."""
    effective_experiment = experiment or _read_apx_agent_config().get("experiment")
    if not effective_experiment:
        raise click.UsageError(
            "Pass --experiment NAME or set [tool.apx.agent].experiment in pyproject.toml."
        )

    from databricks.sdk import WorkspaceClient

    from apx_agent import analyze_canary

    report = analyze_canary(
        endpoint=endpoint,
        experiment=effective_experiment,
        ws=WorkspaceClient(),
        lookback_hours=hours,
    )

    if fmt == "json":
        click.echo(json.dumps({
            "endpoint": report.endpoint,
            "lookback_hours": report.lookback_hours,
            "versions": [
                {
                    "version": v.version,
                    "requests": v.requests,
                    "errors": v.errors,
                    "error_rate": v.error_rate,
                    "latency_p50_ms": v.latency_p50_ms,
                    "latency_p95_ms": v.latency_p95_ms,
                    "latency_avg_ms": v.latency_avg_ms,
                }
                for v in report.versions
            ],
        }, indent=2, default=str))
        return

    click.echo(f"# canary analysis: {report.endpoint} (last {report.lookback_hours}h)")
    if not report.versions:
        click.echo("No traces matched. Either no traffic in the window or "
                   "the served-entity attribute isn't on the traces.")
        return
    click.echo(f"{'VERSION':<12}  {'REQUESTS':>9}  {'ERRORS':>7}  "
               f"{'ERR %':>6}  {'P50 ms':>7}  {'P95 ms':>7}  {'AVG ms':>7}")
    for v in report.versions:
        err_pct = f"{v.error_rate * 100:.1f}" if v.requests else "-"
        avg = f"{v.latency_avg_ms:.0f}" if v.latency_avg_ms is not None else "-"
        p50 = str(v.latency_p50_ms) if v.latency_p50_ms is not None else "-"
        p95 = str(v.latency_p95_ms) if v.latency_p95_ms is not None else "-"
        click.echo(f"{v.version:<12}  {v.requests:>9}  {v.errors:>7}  "
                   f"{err_pct:>6}  {p50:>7}  {p95:>7}  {avg:>7}")
    best_latency = report.best_by_latency()
    best_errors = report.best_by_error_rate()
    if best_latency is not None:
        click.echo(f"\nBest P95 latency: v{best_latency.version} ({best_latency.latency_p95_ms} ms)")
    if best_errors is not None:
        click.echo(f"Best error rate: v{best_errors.version} ({best_errors.error_rate * 100:.2f}%)")


if __name__ == "__main__":
    main()
