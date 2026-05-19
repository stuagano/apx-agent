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
def eval_cmd(evalset: str, module: str, model: str, user_token: str | None) -> None:
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

    result = evaluate(agent, model=model, evalset=data, user_token=user_token)
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
@click.option("--no-deploy", is_flag=True, help="Log + register only, skip databricks.agents.deploy.")
def deploy(module: str, model: str, registered_model_name: str, no_deploy: bool) -> None:
    """Log the agent to MLflow and (optionally) deploy to Model Serving."""
    import mlflow

    from apx_agent import log_agent

    agent = _load_agent(module)
    with mlflow.start_run():
        info = log_agent(
            agent,
            model=model,
            registered_model_name=registered_model_name,
        )
    click.echo(f"Logged {registered_model_name} version {info.registered_model_version}")

    if no_deploy:
        click.echo("Skipping deploy (--no-deploy).")
        return

    try:
        from databricks import agents  # type: ignore[attr-defined]
    except ImportError as e:
        raise click.ClickException(
            "databricks-agents is required for deployment. "
            "Install with: pip install databricks-agents"
        ) from e
    agents.deploy(registered_model_name, model_version=info.registered_model_version)
    click.echo(f"Deployed {registered_model_name} version {info.registered_model_version} as a serving endpoint.")


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


if __name__ == "__main__":
    main()
