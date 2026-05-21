"""apx — command-line interface for the apx-agent framework.

Subcommands:

  apx scaffold <name>           Generate a new agent project
  apx run                       Run the agent locally (uvicorn against create_app)
  apx eval <evalset>            Run Mosaic AI Agent Evaluation
  apx deploy                    Log to MLflow + deploy via databricks.agents.deploy
  apx publish-tools             Publish @tool(uc=...) decorated tools to UC
  apx publish                   Register the deployed endpoint as a Supervisor sub-agent
  apx mcp-config                Emit the Managed MCP client config snippet
  apx memory <cmd>              recall / remember / forget / list — MemoryStore CRUD
  apx examples <cmd>            find / save / remove / list — ExampleStore CRUD
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
import os
import re
import sys
import time
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
# Deploy env-var / secret-scan helpers
# ---------------------------------------------------------------------------

# Env var name pattern that looks like a secret: KEY/TOKEN/SECRET/PASSWORD suffix.
_SECRET_NAME_RE = re.compile(
    r".*(_TOKEN|_KEY|_SECRET|_PASSWORD|_PASS|_PWD)$",
    re.IGNORECASE,
)

# Detect os.environ["X"], os.environ.get("X"), os.getenv("X")
_ENV_REF_RE = re.compile(
    r"""os\.(?:environ\s*\[\s*|environ\.get\s*\(\s*|getenv\s*\(\s*)["']([A-Z][A-Z0-9_]*)["']""",
)


def _looks_like_secret(name: str) -> bool:
    """Return True if ``name`` matches a suspicious env-var naming convention."""
    return bool(_SECRET_NAME_RE.match(name))


def _collect_env_keys_from_dotenv(path: Path) -> set[str]:
    """Read KEY=value lines from a .env-style file, return the set of keys."""
    keys: set[str] = set()
    try:
        text = path.read_text()
    except Exception:
        return keys
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        # Strip a leading 'export ' if present.
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key:
            keys.add(key)
    return keys


def _collect_env_keys_from_pyproject(path: Path) -> set[str]:
    """Pull env-like keys out of a pyproject.toml.

    Conservatively scans for keys that match the secret pattern anywhere in
    the file — both in section headers and as raw string occurrences. This
    is a heuristic; we only use it to drive a warning, never to block.
    """
    keys: set[str] = set()
    try:
        text = path.read_text()
    except Exception:
        return keys
    for match in re.finditer(r"\b([A-Z][A-Z0-9_]{2,})\b", text):
        candidate = match.group(1)
        if _looks_like_secret(candidate):
            keys.add(candidate)
    return keys


def _scan_python_env_refs(root: Path) -> set[str]:
    """Scan .py files under ``root`` for os.environ / os.getenv references.

    Returns the union of referenced env-var names. Skips common virtualenv,
    cache, and build directories.
    """
    skip_dirs = {".venv", "venv", "__pycache__", "build", "dist", ".git", "node_modules"}
    found: set[str] = set()
    if not root.exists():
        return found
    for py in root.rglob("*.py"):
        # Skip anything inside a directory we don't want to walk.
        if any(part in skip_dirs for part in py.parts):
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in _ENV_REF_RE.finditer(text):
            found.add(m.group(1))
    return found


def _run_secret_scan(cwd: Path) -> list[str]:
    """Return suspicious env vars that look like secrets AND are referenced.

    The scan is intentionally conservative: a name only fires the warning
    when it both (a) looks like a secret by naming convention and (b) is
    actually referenced via ``os.environ`` / ``os.getenv`` in the project's
    Python sources. We additionally fold in any names that show up in
    ``.env`` / ``.env.local`` / ``pyproject.toml`` so users get warned about
    credentials they've staged locally.
    """
    referenced = _scan_python_env_refs(cwd)
    declared: set[str] = set()
    for fname in (".env", ".env.local"):
        p = cwd / fname
        if p.exists():
            declared |= _collect_env_keys_from_dotenv(p)
    pyproject = cwd / "pyproject.toml"
    if pyproject.exists():
        declared |= _collect_env_keys_from_pyproject(pyproject)

    suspicious_referenced = {n for n in referenced if _looks_like_secret(n)}
    # Names declared locally that also appear in source.
    declared_in_use = {n for n in declared if n in referenced and _looks_like_secret(n)}
    return sorted(suspicious_referenced | declared_in_use)


class _EnvVarGuard:
    """Context manager that scopes MLFLOW env-var capture controls.

    On entry, sets ``MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING`` to the desired
    value (or leaves it alone). On exit, restores whatever was there before —
    including the absent case. Defensive against polluting the caller shell.
    """

    _KEY = "MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING"

    def __init__(self, capture: bool) -> None:
        self._capture = capture
        self._prev_present = False
        self._prev_value: str | None = None

    def __enter__(self) -> _EnvVarGuard:
        self._prev_present = self._KEY in os.environ
        self._prev_value = os.environ.get(self._KEY)
        # Only force the kill-switch when capture is disabled.
        if not self._capture:
            os.environ[self._KEY] = "false"
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._prev_present:
            # mypy: prev_value is the original string when prev_present is True.
            os.environ[self._KEY] = self._prev_value  # type: ignore[assignment]
        else:
            os.environ.pop(self._KEY, None)


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


# ---------------------------------------------------------------------------
# Databricks Apps scaffold templates
#
# These use ``<APP_NAME>`` placeholders (not ``{name}``) because the
# ``databricks.yml`` body contains literal ``${...}`` bundle interpolation
# that would collide with Python ``.format`` braces. Substitution is done
# with ``str.replace`` instead.
# ---------------------------------------------------------------------------


_SCAFFOLD_APPS_PYPROJECT = '''\
[project]
name = "<APP_NAME>"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    # The [langgraph] extra is REQUIRED at runtime for any app that calls
    # ``compile_to_responses_agent``: it transitively pulls in langchain +
    # langgraph + databricks-langchain, which the responses-agent compiler
    # imports lazily under the hood. A bare ``apx-agent`` dep would let
    # ``uv sync`` succeed but fail at first request inside the deployed App.
    "apx-agent[langgraph]",
    "mlflow[databricks]>=3.0",
    # Add your agent's deps here
]

[project.scripts]
start-server = "agent_server.start_server:main"
quickstart = "scripts.quickstart:main"

[tool.uv.sources]
# Editable parent path keeps `uv sync` working on first checkout. At deploy
# time, `apx deploy --target apps` builds the wheel + rewrites .build/
# pyproject.toml to point at the wheel (since the App container can't
# resolve ../..). If you're installing from PyPI instead, drop this block.
apx-agent = { path = "../..", editable = true }

[tool.apx.agent]
name = "<APP_NAME>"
description = "An apx-agent on Databricks Apps."
model = "databricks-claude-sonnet-4-6"
instructions = "You are a helpful assistant."
'''


_SCAFFOLD_APPS_AGENT = '''\
"""<APP_NAME> — apx-agent on Databricks Apps via mlflow.genai.agent_server.

Generated by ``apx scaffold <APP_NAME> --target apps``.
Edit this file to define your agent. The ``@invoke`` / ``@stream`` decorators
auto-register with the MLflow GenAI Server when this module is imported.
"""
from __future__ import annotations

from apx_agent import Agent, compile_to_responses_agent
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse


# Define your agent. Add tools / sub-agents / memory stores here.
agent = Agent(
    instructions="You are a helpful assistant.",
    tools=[
        # uc_function_tool("main.tools.classify_intent"),
        # genie_tool("abc123", description="Answer data questions"),
    ],
)

LLM_ENDPOINT = "databricks-claude-sonnet-4-6"

# ``compile_to_responses_agent`` returns a ``(non_streaming_fn, streaming_fn)``
# tuple — NOT an object with ``.invoke()`` / ``.stream()`` methods. The two
# callables already accept a ``ResponsesAgentRequest`` and produce a
# ``ResponsesAgentResponse`` (or an iterator of stream events).
_invoke_fn, _stream_fn = compile_to_responses_agent(agent, model=LLM_ENDPOINT)


@invoke()
def non_streaming(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    """Handle a non-streaming ResponsesAgent request."""
    return _invoke_fn(request)


@stream()
def streaming(request: ResponsesAgentRequest):
    """Handle a streaming ResponsesAgent request."""
    yield from _stream_fn(request)
'''


_SCAFFOLD_APPS_START_SERVER = '''\
"""Entry point — runs the MLflow GenAI agent server."""
from __future__ import annotations

from mlflow.genai.agent_server import run

# Import the agent module so the @invoke/@stream decorators register.
from agent_server import agent  # noqa: F401


def main() -> None:
    """Start the MLflow GenAI agent server on 0.0.0.0:8000."""
    run(host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
'''


_SCAFFOLD_APPS_QUICKSTART = '''\
"""Quickstart — local dev setup for the apx-agent Databricks App.

Verifies the Databricks CLI is on PATH, resolves the active profile, creates
an MLflow experiment at the canonical workspace path, and writes a local
``.env`` file with ``DATABRICKS_CONFIG_PROFILE`` + ``MLFLOW_EXPERIMENT_ID``.

Run with: ``uv run quickstart``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


APP_NAME = "<APP_NAME>"


def _require_databricks_cli() -> None:
    """Fail fast if the Databricks CLI is not installed."""
    if shutil.which("databricks") is None:
        sys.stderr.write(
            "databricks CLI not found on PATH. Install it from "
            "https://docs.databricks.com/dev-tools/cli/install.html\\n"
        )
        sys.exit(1)


def _resolve_profile() -> str:
    """Resolve the Databricks profile from env or prompt the user."""
    profile = os.environ.get("DATABRICKS_CONFIG_PROFILE")
    if profile:
        return profile
    profile = input("Databricks CLI profile [DEFAULT]: ").strip() or "DEFAULT"
    return profile


def _current_user(profile: str) -> str:
    """Return the workspace identity of the current Databricks user.

    The workspace expects the *email* form (e.g. ``alice@example.com``) for
    things like ``/Users/<user>/...`` experiment paths — the shell short name
    (``alice``) yields ``NOT_FOUND: Parent directory does not exist``.

    Prefers the ``primary`` entry in the ``emails`` list returned by
    ``databricks current-user me``. Falls back to ``userName`` (often already
    the email, but historically not on every workspace). Falls back to the
    ``USER`` / ``USERNAME`` env vars only when the subprocess itself fails.
    Raises ``RuntimeError`` if no identity could be resolved — silent
    fallback to ``"unknown-user"`` just shifts the failure to MLflow.
    """
    import json

    try:
        result = subprocess.run(
            ["databricks", "current-user", "me",
             "--profile", profile, "--output", "json"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        env_user = os.environ.get("USER") or os.environ.get("USERNAME")
        if env_user:
            return env_user
        raise RuntimeError(
            "Could not resolve Databricks workspace user. "
            "`databricks current-user me` failed and neither USER nor "
            "USERNAME is set in the environment."
        ) from exc

    payload = json.loads(result.stdout)
    emails = payload.get("emails") or []
    if isinstance(emails, list):
        for entry in emails:
            if isinstance(entry, dict) and entry.get("primary") and entry.get("value"):
                return str(entry["value"])
        # No primary flagged — accept the first email with a non-empty value.
        for entry in emails:
            if isinstance(entry, dict) and entry.get("value"):
                return str(entry["value"])
    username = payload.get("userName")
    if isinstance(username, str) and username:
        return username
    raise RuntimeError(
        "`databricks current-user me` succeeded but returned no usable "
        "identity (no emails[].value and no userName)."
    )


def _create_experiment(profile: str, user: str) -> str:
    """Create (or look up) an MLflow experiment at /Users/<user>/<APP_NAME>-dev."""
    import json
    name = f"/Users/{user}/{APP_NAME}-dev"
    # Try create; if it exists, look it up.
    create = subprocess.run(
        ["databricks", "experiments", "create-experiment",
         "--name", name, "--profile", profile, "--output", "json"],
        capture_output=True,
        text=True,
    )
    if create.returncode == 0:
        return json.loads(create.stdout)["experiment_id"]
    lookup = subprocess.run(
        ["databricks", "experiments", "get-by-name",
         "--experiment-name", name, "--profile", profile, "--output", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(lookup.stdout)["experiment"]["experiment_id"]


def _write_env(profile: str, experiment_id: str, experiment_name: str) -> None:
    """Write .env with the resolved Databricks + MLflow settings."""
    env_path = Path(".env")
    lines = [
        f"DATABRICKS_CONFIG_PROFILE={profile}",
        f"MLFLOW_EXPERIMENT_ID={experiment_id}",
        f"MLFLOW_EXPERIMENT_NAME={experiment_name}",
    ]
    env_path.write_text("\\n".join(lines) + "\\n")


def main() -> None:
    """Run the quickstart flow."""
    _require_databricks_cli()
    profile = _resolve_profile()
    user = _current_user(profile)
    experiment_name = f"/Users/{user}/{APP_NAME}-dev"
    experiment_id = _create_experiment(profile, user)
    _write_env(profile, experiment_id, experiment_name)
    sys.stdout.write(
        f"Wrote .env (profile={profile}, experiment_id={experiment_id}).\\n"
    )


if __name__ == "__main__":
    main()
'''


_SCAFFOLD_APPS_DATABRICKS_YML = '''\
bundle:
  name: <APP_NAME>

# The root apx-agent ``.gitignore`` excludes ``**/.build/`` because that
# directory is built from source by ``databricks bundle deploy`` — checking
# it in would leak wheels + duplicated source. But DAB sync honors
# ``.gitignore`` when uploading, which means the staging tree referenced by
# ``source_code_path: ./.build`` would be empty inside the App container.
# ``sync.include`` overrides the ignore for this one path so the bundle can
# upload the staged tree without un-ignoring it everywhere else.
sync:
  include:
    - .build/**

variables:
  workspace_user:
    description: User who owns the experiment
    default: ${workspace.current_user.userName}
  llm_endpoint_name:
    description: Foundation model endpoint the agent calls.
    default: databricks-claude-sonnet-4-6
  mlflow_experiment_id:
    description: |
      MLflow experiment ID for tracing. Populated by scripts/quickstart.py
      into a local .env file and surfaced here for the app environment.
    default: ""

# ``artifacts.default.build`` packages the deploy bundle into ``./.build``.
# ``apx deploy --target apps`` runs this script BEFORE ``bundle validate`` so
# the DAB validator sees a populated source dir. Manually copying the
# apx-agent wheel into the project keeps the App container's ``uv sync``
# off the public index — apx deploy handles the wheel build for you when
# ``[tool.uv.sources].apx-agent`` references a local path.
artifacts:
  default:
    build: |
      mkdir -p .build
      cp -r agent_server scripts pyproject.toml uv.lock README.md .build/ 2>/dev/null || true
      cp apx_agent-*.whl .build/ 2>/dev/null || true

resources:
  experiments:
    <APP_NAME>_experiment:
      name: /Users/${var.workspace_user}/${bundle.name}-${bundle.target}

  apps:
    <APP_NAME>:
      name: <APP_NAME>
      description: <APP_NAME> apx-agent
      source_code_path: ./.build
      resources:
        - name: experiment
          experiment:
            experiment_id: ${resources.experiments.<APP_NAME>_experiment.id}
            permission: CAN_MANAGE
        # apx deploy --target apps will auto-add resources from the agent's
        # ResourceSpec list. For now, list any extras here manually:
        - name: llm-endpoint
          description: Foundation model endpoint used by the agent.
          serving_endpoint:
            name: ${var.llm_endpoint_name}
            permission: CAN_QUERY

      # NOTE: the DAB schema for ``apps.<name>.config`` uses ``env`` (a list
      # of {name, value} dicts) — NOT ``env_variables``. ``bundle validate``
      # warns about unknown keys, so keep this aligned.
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
            value: ${var.llm_endpoint_name}
          - name: MLFLOW_TRACKING_URI
            value: databricks
          - name: MLFLOW_EXPERIMENT_ID
            value: ${var.mlflow_experiment_id}

targets:
  dev:
    mode: development
    default: true
  prod:
    mode: production
    resources:
      apps:
        <APP_NAME>:
          name: <APP_NAME>
'''


_SCAFFOLD_APPS_ENV_EXAMPLE = '''\
DATABRICKS_CONFIG_PROFILE=
MLFLOW_EXPERIMENT_ID=
MLFLOW_EXPERIMENT_NAME=
'''


_SCAFFOLD_APPS_README = '''\
# <APP_NAME>

apx-agent project targeting Databricks Apps.

## Setup
```bash
uv sync
uv run quickstart  # creates the MLflow experiment + writes .env
```

## Local dev
```bash
uv run start-server  # serves on http://localhost:8000
curl -X POST http://localhost:8000/invocations -d '{"input":[{"role":"user","content":"hi"}]}'
```

## Deploy
```bash
apx deploy --target apps  # validates, deploys, runs the bundle
```

## Edit
Define your agent + tools in `agent_server/agent.py`.

> Tip: use underscore/snake_case for `<APP_NAME>` — Databricks bundle
> resource references like `${resources.experiments.<APP_NAME>_experiment.id}`
> are easier to read with snake_case names.
'''


def _scaffold_model_serving(target: Path, name: str, force: bool) -> None:
    """Write the original Model Serving project layout into ``target``.

    Mirrors the pre-``--target`` shape: top-level ``agent.py`` + ``app.py``.
    """
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


def _scaffold_apps(target: Path, name: str, force: bool) -> None:
    """Write a Databricks Apps-ready project layout into ``target``.

    Produces the ``agent_server/`` + ``scripts/`` + ``databricks.yml``
    bundle shape consumed by ``databricks bundle deploy``.
    """
    def _sub(template: str) -> str:
        return template.replace("<APP_NAME>", name)

    files: dict[str, str] = {
        "pyproject.toml": _sub(_SCAFFOLD_APPS_PYPROJECT),
        "databricks.yml": _sub(_SCAFFOLD_APPS_DATABRICKS_YML),
        ".env.example": _SCAFFOLD_APPS_ENV_EXAMPLE,
        ".gitignore": _SCAFFOLD_GITIGNORE,
        "README.md": _sub(_SCAFFOLD_APPS_README),
        "agent_server/__init__.py": "",
        "agent_server/agent.py": _sub(_SCAFFOLD_APPS_AGENT),
        "agent_server/start_server.py": _SCAFFOLD_APPS_START_SERVER,
        "scripts/__init__.py": "",
        "scripts/quickstart.py": _sub(_SCAFFOLD_APPS_QUICKSTART),
    }
    for rel_path, content in files.items():
        path = target / rel_path
        if path.exists() and not force:
            click.echo(f"  skip   {path} (exists; pass --force to overwrite)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        click.echo(f"  write  {path}")


@main.command()
@click.argument("name")
@click.option(
    "--dir", "directory",
    default=".",
    type=click.Path(file_okay=False),
    help="Target directory. Default: current directory.",
)
@click.option(
    "--target", "scaffold_target",
    type=click.Choice(["model-serving", "apps"]),
    default="model-serving",
    show_default=True,
    help=(
        "Runtime to generate scaffolding for. "
        "'model-serving' (default, backwards-compatible) generates the "
        "flat agent.py + app.py layout deployed via apx deploy. "
        "'apps' generates a Databricks Asset Bundle layout with "
        "agent_server/ + databricks.yml deployed via apx deploy --target apps."
    ),
)
@click.option("--force", is_flag=True, help="Overwrite existing files.")
def scaffold(name: str, directory: str, scaffold_target: str, force: bool) -> None:
    """Generate a new agent project at <NAME>.

    With ``--target model-serving`` (the default) writes a flat
    ``agent.py``/``app.py`` project. With ``--target apps`` writes a
    Databricks Apps bundle: ``agent_server/`` package + ``databricks.yml``,
    deployable with ``apx deploy --target apps`` (or directly via
    ``databricks bundle deploy``).
    """
    target = Path(directory) / name
    if target.exists() and not force:
        if any(target.iterdir()):
            raise click.ClickException(
                f"{target} already exists and is not empty. Pass --force to overwrite."
            )
    target.mkdir(parents=True, exist_ok=True)

    if scaffold_target == "apps":
        _scaffold_apps(target, name, force)
    else:
        _scaffold_model_serving(target, name, force)

    click.echo()
    click.echo(f"Scaffolded {name} at {target} (target={scaffold_target}).")
    if scaffold_target == "apps":
        click.echo(f"Next: cd {name} && uv sync && uv run quickstart && uv run start-server")
    else:
        click.echo(f"Next: cd {name} && uv sync && apx run")


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
@click.option("--module", default=None, help='Agent module spec. Default '
              '"agent:agent" for --target model-serving; '
              '"agent_server.agent:agent" for --target apps.')
@click.option(
    "--target", "target", default="model-serving",
    type=click.Choice(["model-serving", "apps"]),
    help="Deployment target. 'model-serving' (default) runs the canonical "
         "log_agent + databricks.agents.deploy flow. 'apps' runs the "
         "Databricks Asset Bundle deploy + run flow against a scaffolded "
         "Apps project. The two targets accept different option sets — "
         "see --help for details.",
)
@click.option("--model", default=None, help="Databricks serving endpoint for "
              "the LLM. Required when --target model-serving.")
@click.option(
    "--name", "registered_model_name", default=None,
    help="UC three-part name to register the model under (catalog.schema.model). "
         "Required when --target model-serving.",
)
@click.option(
    "--profile", default=None,
    help="Databricks CLI profile to pass through to `databricks bundle ...` "
         "and `databricks apps ...`. Only used by --target apps.",
)
@click.option(
    "--bundle-target", default="dev",
    help="DAB target name to deploy and run under. Only used by --target apps. "
         "Default: dev.",
)
@click.option(
    "--no-run", is_flag=True,
    help="Skip the `databricks bundle run <app>` step. Only used by --target apps.",
)
@click.option(
    "--auto-update-yml", is_flag=True, default=False,
    help="Walk the agent's resource tree and merge missing ResourceSpec "
         "entries into databricks.yml under resources.apps.<app>.resources. "
         "User-added resources with a matching name are NEVER clobbered. "
         "Only used by --target apps.",
)
@click.option(
    "--auto-build-wheel/--no-auto-build-wheel", default=True,
    help="When [tool.uv.sources].apx-agent in pyproject.toml points at a "
         "local wheel that doesn't exist (or an editable parent path), "
         "walk up to find the apx-agent source root, run `uv build --wheel`, "
         "copy the result into cwd, execute the bundle's artifacts.default.build "
         "script to populate .build/, and (for editable sources) rewrite "
         ".build/pyproject.toml to use the wheel path. ON by default. Only "
         "used by --target apps.",
)
@click.option(
    "--auto-experiment/--no-auto-experiment", default=True,
    help="Auto-resolve an MLflow experiment id for the deploy: if the "
         "caller didn't pass --var mlflow_experiment_id=, look up "
         "/Users/<current-user>/<app_name>-<target> and create it if "
         "missing, then pass it through to bundle deploy + bundle run. "
         "ON by default. Only used by --target apps.",
)
@click.option(
    "--var", "vars", multiple=True,
    help="Extra `--var key=value` pairs to forward to `databricks bundle "
         "deploy + bundle run`. Repeatable. Use to override resources, "
         "wire in a vector_search_index, etc.",
)
@click.option(
    "--json-output", is_flag=True, default=False,
    help="Emit a single JSON object on stdout summarising the run. "
         "Progress logs are routed to stderr.",
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
@click.option(
    "--capture-env-vars/--no-capture-env-vars", default=False,
    help="Allow MLflow to record env vars referenced in the agent's dependency "
         "chain into the logged model artifact. OFF by default to prevent "
         "developer-shell secrets (ATLASSIAN_API_KEY, GEMINI_API_KEY, etc.) "
         "from being baked into the deployed image. Sets "
         "MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING=false for the duration of "
         "log_agent and restores the caller's environment afterward.",
)
@click.option(
    "--allow-env-var", "allow_env_vars", multiple=True, metavar="NAME",
    help="Explicitly allow a specific env var to be recorded. Repeatable: "
         "--allow-env-var DATABRICKS_HOST --allow-env-var DATABRICKS_TOKEN. "
         "Note: MLflow's MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING is all-or-nothing; "
         "passing any --allow-env-var flips capture back on for everything and "
         "prints a warning listing what you asked to allow vs the full capture "
         "set you'll actually get.",
)
@click.option(
    "--yes", "-y", "assume_yes", is_flag=True,
    help="Skip the secret-scan confirmation prompt. Use in CI / scripts.",
)
def deploy(
    module: str | None,
    target: str,
    model: str | None,
    registered_model_name: str | None,
    profile: str | None,
    bundle_target: str,
    no_run: bool,
    auto_update_yml: bool,
    auto_build_wheel: bool,
    auto_experiment: bool,
    vars: tuple[str, ...],
    json_output: bool,
    no_deploy: bool,
    experiment: str | None,
    publish_tools: bool,
    set_uc_tags: bool,
    agent_name: str | None,
    capture_env_vars: bool,
    allow_env_vars: tuple[str, ...],
    assume_yes: bool,
) -> None:
    """Log the agent to MLflow + deploy + UC-tag in one command.

    With ``--target model-serving`` (default) runs the canonical flow:

      1. publish_tools_to_uc(agent)    — register any @tool(uc=...) tools
      2. log_agent(agent, ...)         — log to MLflow + register in UC
      3. databricks.agents.deploy(...) — promote to a serving endpoint
      4. set_uc_tags_for_agent(...)    — write apx.agent.* tags

    Toggle individual stages with --no-publish-tools, --no-deploy, or
    --no-set-uc-tags.

    With ``--target apps`` runs the Databricks Asset Bundle deploy flow:

      1. databricks bundle validate    — sanity check the bundle config
      2. databricks bundle deploy      — push the app + sync resources
      3. databricks bundle run <app>   — start the app (skipped with --no-run)
      4. databricks apps get           — poll until ACTIVE/RUNNING

    Note: the auto-derived UC tags / publish-tools flow does not currently
    apply to ``--target apps`` (no model version to tag). Apps tagging will
    be addressed in a follow-up.
    """
    if target == "apps":
        _deploy_apps(
            module=module or "agent_server.agent:agent",
            profile=profile,
            bundle_target=bundle_target,
            no_run=no_run,
            auto_update_yml=auto_update_yml,
            auto_build_wheel=auto_build_wheel,
            auto_experiment=auto_experiment,
            vars=vars,
            json_output=json_output,
        )
        return

    # --- model-serving target (the legacy path) ---
    if model is None:
        raise click.UsageError(
            "--model is required when --target model-serving (the default). "
            "Pass --target apps to use the Databricks Apps flow instead."
        )
    if registered_model_name is None:
        raise click.UsageError(
            "--name is required when --target model-serving (the default). "
            "Pass --target apps to use the Databricks Apps flow instead."
        )
    effective_module = module or "agent:agent"

    import mlflow

    from apx_agent import log_agent

    # Resolve effective env-var-capture policy.
    #
    #   --no-capture-env-vars (default) + no --allow-env-var → capture OFF.
    #   --capture-env-vars                                   → capture ON.
    #   --allow-env-var X (with --no-capture-env-vars)       → BadParameter.
    #   --allow-env-var X (with --capture-env-vars)          → capture ON,
    #     warn that MLflow capture is all-or-nothing.
    if allow_env_vars and not capture_env_vars:
        raise click.BadParameter(
            "--allow-env-var implies env-var capture is enabled, but "
            "--no-capture-env-vars (the default) is set. Either drop "
            "--allow-env-var, or pass --capture-env-vars explicitly.",
            param_hint="--allow-env-var",
        )
    effective_capture = capture_env_vars
    if allow_env_vars:
        click.echo(
            "# WARNING: MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING is "
            "all-or-nothing in MLflow today. You asked to allow only "
            f"{list(allow_env_vars)}, but capture will record EVERY env var "
            "MLflow detects in the dependency chain.",
            err=True,
        )

    # Pre-flight: tell the user what mode we're running in.
    click.echo(
        f"Logging with: env-var-capture="
        f"{'on' if effective_capture else 'off'}, secrets-scan=on",
    )

    # Pre-flight: secret scan.
    suspicious = _run_secret_scan(Path.cwd())
    if suspicious and effective_capture:
        click.echo(
            "# WARNING: env-var capture is ON and the following names "
            "look like secrets referenced in this project:",
            err=True,
        )
        for name in suspicious:
            click.echo(f"#   - {name}", err=True)
        if not assume_yes:
            click.confirm(
                "Continue with capture enabled?",
                default=False, abort=True,
            )
    elif suspicious:
        # Capture is off, but still surface what we noticed so the user
        # has a record of what would have leaked under the old behavior.
        click.echo(
            "# Note: secret-looking env vars detected in project sources "
            "(capture is OFF so these will NOT be baked into the model):",
            err=True,
        )
        for name in suspicious:
            click.echo(f"#   - {name}", err=True)

    agent = _load_agent(effective_module)
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
    if effective_experiment:
        mlflow.set_experiment(effective_experiment)
    with _EnvVarGuard(capture=effective_capture), mlflow.start_run():
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
# deploy --target apps
# ---------------------------------------------------------------------------


def _run_databricks_cmd(
    args: list[str], profile: str | None = None,
) -> Any:
    """Invoke the ``databricks`` CLI with ``args`` (and optional profile).

    A single seam through which every Databricks CLI call routes so tests can
    mock subprocess execution by patching this helper. Returns the
    ``subprocess.CompletedProcess`` (stdout/stderr captured, text mode).
    """
    import subprocess

    cmd = ["databricks", *args]
    if profile:
        cmd.extend(["--profile", profile])
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _tail_lines(text: str, n: int = 50) -> str:
    """Return the last ``n`` lines of ``text`` (joined)."""
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _read_databricks_yml(cwd: Path) -> dict[str, Any]:
    """Load ``databricks.yml`` from ``cwd`` and return the parsed dict.

    Raises ``click.ClickException`` with a friendly message if the file is
    missing or doesn't parse.
    """
    import yaml

    path = cwd / "databricks.yml"
    if not path.exists():
        raise click.ClickException(
            f"No databricks.yml found at {path}. "
            "--target apps expects a scaffolded Apps project — run "
            "`apx scaffold <name> --target apps` first."
        )
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise click.ClickException(f"Failed to parse databricks.yml: {e}") from e
    if not isinstance(data, dict):
        raise click.ClickException(
            f"databricks.yml does not contain a mapping at the top level "
            f"(got {type(data).__name__})."
        )
    return data


def _resolve_app_name(bundle_doc: dict[str, Any]) -> tuple[str, str]:
    """Return ``(bundle_key, app_name)`` from a bundle document.

    The Databricks Asset Bundle schema lets the YAML key under
    ``resources.apps`` differ from the actual workspace app name. For example::

        resources:
          apps:
            entity-resolution-agent-app:      # ← bundle key
              name: "entity-resolution-agent" # ← workspace app name

    ``databricks bundle run <bundle_key>`` operates on the bundle key.
    ``databricks apps get <app_name>`` operates on the workspace app name.
    Callers must thread the right identifier to the right call.

    If the app's block omits ``name:``, the bundle key is used as the app
    name (this matches the DAB default).

    Errors out if 0 or >1 apps are declared — the Apps deploy flow is
    single-app by convention.
    """
    resources = bundle_doc.get("resources") or {}
    apps = resources.get("apps") if isinstance(resources, dict) else None
    if not isinstance(apps, dict) or not apps:
        raise click.ClickException(
            "databricks.yml has no `resources.apps.<name>` entry. "
            "--target apps requires exactly one app declared in the bundle."
        )
    if len(apps) > 1:
        raise click.ClickException(
            f"databricks.yml declares multiple apps ({sorted(apps)}). "
            "--target apps requires exactly one."
        )
    bundle_key = next(iter(apps))
    block = apps[bundle_key] or {}
    app_name = bundle_key
    if isinstance(block, dict) and isinstance(block.get("name"), str):
        app_name = block["name"]
    return bundle_key, app_name


def _ensure_apx_wheel(cwd: Path) -> Path | None:
    """Build + stage the apx-agent wheel when pyproject.toml references one.

    Logic:

    1. Read ``pyproject.toml`` in ``cwd``. Look at
       ``[tool.uv.sources].apx-agent``.
    2. If the source is a path (``{ path = "./apx_agent-X.Y.Z.whl" }``):
       - If the wheel exists at that path already, return it (nothing to do).
       - Otherwise, walk up from ``cwd`` to find the apx-agent source root
         (parent dir containing ``src/apx_agent/__init__.py`` AND a
         ``pyproject.toml`` where ``[project].name == "apx-agent"``).
       - Run ``uv build --wheel`` there, then copy the freshly-built wheel
         from ``<source_root>/dist/`` into ``cwd``.
       - If the source root's version doesn't match the wheel filename the
         pyproject points at, raise ``click.ClickException`` (don't silently
         produce a broken deploy — the App container's ``uv sync`` would
         fail trying to install a non-existent file path).
    3. If ``[tool.uv.sources]`` doesn't reference apx-agent at all, return
       ``None`` (registry installs work fine inside the App container, no
       wheel staging needed).

    Failures (build error, no source root found, version mismatch) raise
    ``click.ClickException`` with a clear message + the manual commands
    the user can run to bypass.
    """
    import shutil
    import subprocess

    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]

    pyproject_path = cwd / "pyproject.toml"
    if not pyproject_path.exists():
        return None
    try:
        doc = tomllib.loads(pyproject_path.read_text())
    except Exception as e:
        raise click.ClickException(
            f"Failed to parse pyproject.toml at {pyproject_path}: {e}"
        ) from e

    sources = (
        doc.get("tool", {}).get("uv", {}).get("sources", {})
    )
    apx_source = sources.get("apx-agent") if isinstance(sources, dict) else None
    if not isinstance(apx_source, dict):
        # No path/editable override — registry install handles it.
        return None
    raw_path = apx_source.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        # Some other override (git, url, workspace). We don't manage those.
        return None

    # Two shapes of [tool.uv.sources].apx-agent we recognise:
    #
    # 1. Wheel-pinned: { path = "./apx_agent-X.Y.Z.whl" } — deploy-correct
    #    pyproject. We build the wheel if missing.
    #
    # 2. Editable parent: { path = "../..", editable = true } — local-dev
    #    correct pyproject. We build the wheel + return the path so the
    #    caller can rewrite .build/pyproject.toml to use the wheel for
    #    the deployed container.
    raw_target = (cwd / raw_path).resolve()
    expected_name: str | None = None
    is_editable_dir = bool(apx_source.get("editable")) or raw_target.is_dir()

    if is_editable_dir:
        # raw_target should be the apx-agent source root. Resolve version
        # from its pyproject and synthesize the expected wheel filename.
        sub_pyproject = raw_target / "pyproject.toml"
        if sub_pyproject.exists():
            try:
                sub_doc = tomllib.loads(sub_pyproject.read_text())
                src_ver = str(sub_doc.get("project", {}).get("version") or "")
            except Exception:
                src_ver = ""
        else:
            src_ver = ""
        if src_ver:
            expected_name = f"apx_agent-{src_ver}-py3-none-any.whl"
            wheel_target = cwd / expected_name
        else:
            wheel_target = cwd / "apx_agent-0.0.0-py3-none-any.whl"  # fallback
    else:
        # Wheel-pinned shape.
        wheel_target = raw_target
        expected_name = wheel_target.name

    if wheel_target.exists() and not is_editable_dir:
        click.echo(
            f"  apx-agent wheel already staged: {wheel_target.name}", err=True,
        )
        return wheel_target

    # Wheel is missing — locate the apx-agent source root.
    source_root: Path | None = None
    if is_editable_dir and raw_target.is_dir():
        # The editable path IS the source root (validated by checking for
        # the marker + apx-agent name).
        marker = raw_target / "src" / "apx_agent" / "__init__.py"
        sub_pyproject = raw_target / "pyproject.toml"
        if marker.exists() and sub_pyproject.exists():
            try:
                sub_doc = tomllib.loads(sub_pyproject.read_text())
                if sub_doc.get("project", {}).get("name") == "apx-agent":
                    source_root = raw_target
            except Exception:
                pass
    if source_root is None:
        # Walk up from cwd as a fallback (covers the wheel-pinned shape).
        for parent in (cwd, *cwd.parents):
            marker = parent / "src" / "apx_agent" / "__init__.py"
            sub_pyproject = parent / "pyproject.toml"
            if not (marker.exists() and sub_pyproject.exists()):
                continue
            try:
                sub_doc = tomllib.loads(sub_pyproject.read_text())
            except Exception:
                continue
            if sub_doc.get("project", {}).get("name") == "apx-agent":
                source_root = parent
                break

    if source_root is None:
        raise click.ClickException(
            "pyproject.toml references a local apx-agent wheel at "
            f"{raw_path!r} but no wheel was found there, and no apx-agent "
            "source root could be located by walking up from the current "
            "directory.\n\n"
            "Manual workaround:\n"
            "  cd <apx-agent source root>\n"
            "  uv build --wheel\n"
            f"  cp dist/{expected_name} {cwd}/\n"
            "Or re-run with --no-auto-build-wheel after staging the wheel "
            "yourself."
        )

    # Sanity-check the source root version against the wheel filename so we
    # don't silently produce a deploy where pyproject.toml points at a wheel
    # that doesn't exist after the build.
    src_version = source_root.joinpath("pyproject.toml")
    try:
        src_doc = tomllib.loads(src_version.read_text())
        src_ver = str(src_doc.get("project", {}).get("version") or "")
    except Exception:
        src_ver = ""
    if src_ver and src_ver not in expected_name:
        raise click.ClickException(
            f"apx-agent source root at {source_root} has version "
            f"{src_ver!r}, but pyproject.toml at {pyproject_path} "
            f"references {expected_name!r}.\n\n"
            "Update [tool.uv.sources].apx-agent.path to point at "
            f"./apx_agent-{src_ver}-py3-none-any.whl, then re-run apx "
            "deploy."
        )

    click.echo(f"  uv build --wheel (in {source_root})", err=True)
    proc = subprocess.run(
        ["uv", "build", "--wheel"],
        cwd=str(source_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise click.ClickException(
            f"`uv build --wheel` failed in {source_root} "
            f"(exit {proc.returncode}).\n\n"
            f"stderr tail:\n{_tail_lines(proc.stderr or proc.stdout)}\n\n"
            "Manual workaround:\n"
            f"  cd {source_root}\n"
            "  uv build --wheel\n"
            f"  cp dist/{expected_name} {cwd}/"
        )

    dist_dir = source_root / "dist"
    built = dist_dir / expected_name
    if not built.exists():
        # The build succeeded but the produced wheel doesn't match what
        # pyproject.toml expects. List what's actually in dist/ so the
        # user can spot the mismatch.
        found = sorted(p.name for p in dist_dir.glob("apx_agent-*.whl"))
        raise click.ClickException(
            f"`uv build --wheel` produced wheels in {dist_dir} but none "
            f"matched the expected name {expected_name!r}.\n"
            f"Found: {found}\n"
            f"Update [tool.uv.sources].apx-agent.path in {pyproject_path} "
            "to point at one of these filenames, then re-run."
        )

    shutil.copy2(built, wheel_target)
    return wheel_target


def _run_bundle_artifacts(cwd: Path) -> None:
    """Execute the bundle's ``artifacts.default.build`` script before validate.

    ``databricks bundle validate`` fails with ``stat .build: no such file
    or directory`` when artifacts haven't been pre-built. The artifacts
    block DOES run on ``bundle deploy``, but we want validation to also
    pass — and the build script also needs to copy the apx-agent wheel
    we just built into ``.build/``.

    Implementation: read ``databricks.yml``, walk to
    ``artifacts.default.build``, extract the bash script body, execute
    it via ``bash -c`` with ``cwd=cwd``. On failure, raise
    ``click.ClickException`` with the script output. If the
    ``artifacts.default.build`` block is missing, return silently — the
    project doesn't need pre-build.
    """
    import subprocess

    doc = _read_databricks_yml(cwd)
    artifacts = doc.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        return
    default = artifacts.get("default") or {}
    if not isinstance(default, dict):
        return
    script = default.get("build")
    if not isinstance(script, str) or not script.strip():
        return

    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise click.ClickException(
            "`artifacts.default.build` script failed "
            f"(exit {proc.returncode}).\n\n"
            f"stderr tail:\n{_tail_lines(proc.stderr or proc.stdout)}\n\n"
            "Re-run with --no-auto-build-wheel if you'd rather pre-build "
            "the .build directory by hand."
        )


def _rewrite_build_pyproject_for_deploy(
    build_dir: Path, wheel_path: Path,
) -> None:
    """Rewrite ``.build/pyproject.toml`` to point at the bundled wheel.

    The example's source pyproject can use editable
    ``[tool.uv.sources].apx-agent = { path = "../..", editable = true }``
    so ``uv sync`` works on first checkout. The deployed App container
    can't resolve ``../..``, so this helper rewrites the staged copy in
    ``.build/`` to use the local wheel path instead.

    Idempotent: skips silently if ``.build/pyproject.toml`` already points
    at a wheel, or if it has no ``[tool.uv.sources].apx-agent`` entry.

    After rewriting, regenerates ``.build/uv.lock`` against the new
    pyproject so the App container's ``uv sync`` resolves cleanly.
    """
    import subprocess

    try:
        import tomllib  # py>=3.11
    except ImportError:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]

    pyproject = build_dir / "pyproject.toml"
    if not pyproject.exists():
        return
    text = pyproject.read_text()
    try:
        doc = tomllib.loads(text)
    except Exception:
        return

    sources = doc.get("tool", {}).get("uv", {}).get("sources", {})
    apx_source = sources.get("apx-agent") if isinstance(sources, dict) else None
    if not isinstance(apx_source, dict):
        return
    current_path = apx_source.get("path")
    if not isinstance(current_path, str):
        return
    # Already wheel-pinned? No-op.
    if current_path.endswith(".whl"):
        return

    # Replace whichever line carries the apx-agent source. We avoid
    # rewriting the whole TOML to preserve comments + ordering.
    wheel_rel = f"./{wheel_path.name}"
    new_block = f'apx-agent = {{ path = "{wheel_rel}" }}'
    lines = text.splitlines()
    swapped = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("apx-agent ") and "=" in stripped and "{" in stripped:
            lines[i] = new_block
            swapped = True
            break
    if not swapped:
        return
    pyproject.write_text("\n".join(lines) + "\n")
    click.echo(
        f"  rewrote .build/pyproject.toml: apx-agent -> {wheel_rel}", err=True,
    )

    # Make sure the wheel is staged in .build/ alongside the pyproject.
    target_wheel = build_dir / wheel_path.name
    if not target_wheel.exists():
        import shutil
        shutil.copy2(wheel_path, target_wheel)

    # Regenerate uv.lock inside .build/ against the rewritten pyproject.
    lockfile = build_dir / "uv.lock"
    if lockfile.exists():
        lockfile.unlink()
    proc = subprocess.run(
        ["uv", "lock"], cwd=str(build_dir),
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise click.ClickException(
            f"`uv lock` failed in {build_dir} after rewriting pyproject "
            f"(exit {proc.returncode}).\nstderr tail:\n"
            f"{_tail_lines(proc.stderr or proc.stdout)}"
        )
    click.echo("  regenerated .build/uv.lock", err=True)


def _ensure_experiment_id(
    profile: str | None,
    bundle_name: str,
    bundle_target: str,
    env_value: str | None,
) -> str | None:
    """Resolve an MLflow experiment id for `--var mlflow_experiment_id=...`.

    Lookup order:
      1. ``env_value`` (anything the caller already provided via --var)
      2. Local ``.env`` if it has ``MLFLOW_EXPERIMENT_ID``
      3. Look up by canonical path ``/Users/<user>/<bundle_name>-<target>``;
         create it if missing.
      4. Return ``None`` on failure (deploy proceeds without experiment).
    """
    import json as _json
    import subprocess

    if env_value:
        return env_value

    # Step 2: .env in cwd
    env_file = Path.cwd() / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("MLFLOW_EXPERIMENT_ID="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val

    # Step 3: lookup / create via databricks CLI
    cmd_user = ["databricks", "current-user", "me", "--output", "json"]
    if profile:
        cmd_user += ["--profile", profile]
    try:
        proc = subprocess.run(cmd_user, capture_output=True, text=True, check=True)
        me = _json.loads(proc.stdout)
    except Exception as exc:
        click.echo(
            f"# could not resolve current user for experiment auto-create: {exc}",
            err=True,
        )
        return None

    email = None
    for entry in me.get("emails") or []:
        if entry.get("primary") and entry.get("value"):
            email = entry["value"]
            break
    email = email or me.get("userName")
    if not email:
        return None

    exp_path = f"/Users/{email}/{bundle_name}-{bundle_target}"

    # get-by-name (use --json to suppress the auto-error on miss)
    get_cmd = [
        "databricks", "experiments", "get-by-name", exp_path, "--output", "json",
    ]
    if profile:
        get_cmd += ["--profile", profile]
    proc = subprocess.run(get_cmd, capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        try:
            data = _json.loads(proc.stdout)
            eid = data.get("experiment", {}).get("experiment_id") or data.get("experiment_id")
            if eid:
                click.echo(f"  reusing experiment: {exp_path} (id={eid})", err=True)
                return str(eid)
        except Exception:
            pass

    # Create
    create_cmd = ["databricks", "experiments", "create-experiment", exp_path, "--output", "json"]
    if profile:
        create_cmd += ["--profile", profile]
    proc = subprocess.run(create_cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        click.echo(
            f"# experiment create failed for {exp_path}: "
            f"{_tail_lines(proc.stderr or proc.stdout, 3)}",
            err=True,
        )
        return None
    try:
        data = _json.loads(proc.stdout)
        eid = data.get("experiment_id")
        if eid:
            click.echo(f"  created experiment: {exp_path} (id={eid})", err=True)
            return str(eid)
    except Exception:
        pass
    return None


def _preflight_apps(cwd: Path) -> None:
    """Verify the cwd looks like a scaffolded Apps project.

    Checks for ``databricks.yml``, ``pyproject.toml``, and ``agent_server/``.
    Raises ``click.ClickException`` with a friendly message on the first
    missing piece.
    """
    missing: list[str] = []
    if not (cwd / "databricks.yml").exists():
        missing.append("databricks.yml")
    if not (cwd / "pyproject.toml").exists():
        missing.append("pyproject.toml")
    if not (cwd / "agent_server").is_dir():
        missing.append("agent_server/")
    if missing:
        raise click.ClickException(
            "Pre-flight failed for --target apps. Missing in current "
            f"directory: {', '.join(missing)}. Run `apx scaffold <name> "
            "--target apps` to generate the expected layout."
        )


def _validate_responses_agent_compiler() -> None:
    """Confirm the apps extra is installed.

    The Apps deploy flow ultimately relies on ``compile_to_responses_agent``
    being available at runtime in the deployed image. We surface a friendly
    error if the apx-agent[apps] extra (mlflow.genai) isn't installed in
    the developer environment.
    """
    try:
        from apx_agent._responses_agent import compile_to_responses_agent  # noqa: F401
    except ImportError as e:
        raise click.ClickException(
            "compile_to_responses_agent is not available — please install "
            "the apps extra: pip install 'apx-agent[apps]'. "
            f"(underlying error: {e})"
        ) from e


def _auto_update_databricks_yml(
    cwd: Path,
    *,
    agent: Any,
    bundle_key: str,
    log: Any,
) -> tuple[list[str], list[str]]:
    """Merge missing ResourceSpec entries into databricks.yml.

    Returns ``(added_names, skipped_names)``. The bundle document is read,
    each ResourceSpec is mapped to a DAB resource entry via
    ``resources_to_databricks_yml``, and any entry whose ``name`` is not
    already present in ``resources.apps.<bundle_key>.resources`` is appended.
    User-added entries with the same name are NEVER clobbered.
    """
    import yaml

    from apx_agent._resources import (
        collect_resource_specs,
        resources_to_databricks_yml,
    )

    path = cwd / "databricks.yml"
    doc = _read_databricks_yml(cwd)

    apps_block = doc.setdefault("resources", {}).setdefault("apps", {})
    if bundle_key not in apps_block or not isinstance(apps_block[bundle_key], dict):
        raise click.ClickException(
            f"databricks.yml has no resources.apps.{bundle_key} block — "
            "cannot auto-update."
        )
    app_block = apps_block[bundle_key]
    existing: list[dict[str, Any]] = app_block.get("resources") or []

    def _entry_name(entry: dict[str, Any]) -> str | None:
        """Extract the ``name`` from a DAB resource entry.

        The DAB shape is ``{"<resource_type>": {"name": "...", ...}}``;
        each entry has exactly one top-level key, with ``name`` nested
        inside. We also accept the older flattened form
        ``{"name": ..., "<resource_type>": {...}}`` for forward-compat.
        """
        if not isinstance(entry, dict):
            return None
        if isinstance(entry.get("name"), str):
            return entry["name"]
        for v in entry.values():
            if isinstance(v, dict) and isinstance(v.get("name"), str):
                return v["name"]
        return None

    existing_names = {
        n for n in (_entry_name(e) for e in existing) if n is not None
    }

    specs = collect_resource_specs(agent)
    added: list[str] = []
    skipped: list[str] = []
    for spec in specs:
        # Render one entry at a time so the test of "merge with existing"
        # works on the same shape as a full render.
        rendered = resources_to_databricks_yml([spec])
        if not rendered:
            continue
        entry = rendered[0]
        name = _entry_name(entry)
        if name is None:
            # Unexpected shape — skip rather than crash.
            continue
        if name in existing_names:
            skipped.append(name)
            continue
        existing.append(entry)
        existing_names.add(name)
        added.append(name)

    app_block["resources"] = existing
    path.write_text(yaml.safe_dump(doc, default_flow_style=False, sort_keys=False))

    if added:
        log(f"  auto-added {len(added)} resources: {', '.join(added)}")
    if skipped:
        log(f"  skipped {len(skipped)} resources already declared: "
            f"{', '.join(skipped)}")
    if not added and not skipped:
        log("  no resources to merge (agent declared none)")
    return added, skipped


def _poll_app_ready(
    app_name: str,
    profile: str | None,
    *,
    timeout_seconds: int = 300,
    log: Any,
) -> dict[str, Any]:
    """Poll ``databricks apps get`` until the app is ACTIVE + RUNNING.

    Exponential backoff between attempts, capped at 15s. Fails fast on
    terminal failure states (``ERROR`` / ``CRASHED``). Raises
    ``click.ClickException`` on timeout or terminal failure. Returns the
    final parsed JSON payload on success.
    """
    import json as _json

    deadline = time.time() + timeout_seconds
    delay = 1.0
    last_state: tuple[str, str] = ("", "")
    while True:
        proc = _run_databricks_cmd(
            ["apps", "get", app_name, "-o", "json"], profile=profile,
        )
        if proc.returncode != 0:
            # Don't bail immediately — the app may not be visible yet
            # right after deploy. Treat first few non-zero exits as transient.
            log(f"  (apps get returned {proc.returncode}; retrying)")
        else:
            try:
                payload = _json.loads(proc.stdout)
            except _json.JSONDecodeError:
                payload = {}
            compute_state = (
                (payload.get("compute_status") or {}).get("state") or ""
            ).upper()
            app_state = (
                (payload.get("app_status") or {}).get("state") or ""
            ).upper()
            if (compute_state, app_state) != last_state:
                log(f"  status: compute={compute_state or '?'} "
                    f"app={app_state or '?'}")
                last_state = (compute_state, app_state)
            if compute_state == "ACTIVE" and app_state == "RUNNING":
                return payload
            if compute_state == "ERROR" or app_state == "CRASHED":
                raise click.ClickException(
                    f"App {app_name!r} entered a terminal failure state "
                    f"(compute={compute_state}, app={app_state}). "
                    "Inspect logs via `databricks apps logs`."
                )

        if time.time() >= deadline:
            raise click.ClickException(
                f"Timed out after {timeout_seconds}s waiting for app "
                f"{app_name!r} to reach ACTIVE/RUNNING. "
                "Last observed state: "
                f"compute={last_state[0] or '?'} app={last_state[1] or '?'}."
            )
        time.sleep(min(delay, max(0.0, deadline - time.time())))
        delay = min(delay * 1.5, 15.0)


def _deploy_apps(
    *,
    module: str,
    profile: str | None,
    bundle_target: str,
    no_run: bool,
    auto_update_yml: bool,
    auto_build_wheel: bool,
    auto_experiment: bool = True,
    vars: tuple[str, ...] = (),
    json_output: bool,
) -> None:
    """Implement ``apx deploy --target apps``.

    Routes all progress logs to stderr (so ``--json-output`` can keep stdout
    clean), runs the bundle validate → deploy → run → poll-ready sequence,
    and prints either the app URL (default) or a single JSON summary
    (``--json-output``) at the end.
    """
    cwd = Path.cwd()

    def log(msg: str) -> None:
        click.echo(msg, err=True)

    try:
        _deploy_apps_impl(
            cwd=cwd, module=module, profile=profile,
            bundle_target=bundle_target, no_run=no_run,
            auto_update_yml=auto_update_yml,
            auto_build_wheel=auto_build_wheel,
            auto_experiment=auto_experiment,
            vars=vars,
            json_output=json_output, log=log,
        )
    except click.ClickException as e:
        if json_output:
            click.echo(json.dumps({"ok": False, "error": str(e)}))
            raise click.exceptions.Exit(1) from e
        raise


def _deploy_apps_impl(
    *,
    cwd: Path,
    module: str,
    profile: str | None,
    bundle_target: str,
    no_run: bool,
    auto_update_yml: bool,
    auto_build_wheel: bool,
    auto_experiment: bool = True,
    vars: tuple[str, ...] = (),
    json_output: bool,
    log: Any,
) -> None:
    """Inner body of ``_deploy_apps`` — see docstring there."""
    log(f"# apx deploy --target apps (bundle-target={bundle_target}, "
        f"profile={profile or '<default>'})")

    # 1. Pre-flight
    _preflight_apps(cwd)
    _validate_responses_agent_compiler()
    doc = _read_databricks_yml(cwd)
    bundle_key, app_name = _resolve_app_name(doc)
    if bundle_key != app_name:
        log(f"# resolved bundle_key={bundle_key} app_name={app_name}")
    else:
        log(f"# resolved app_name: {app_name}")

    # 2. Optional auto-merge resources
    if auto_update_yml:
        log("# auto-update-yml: merging agent ResourceSpec into databricks.yml")
        agent = _load_agent(module)
        _auto_update_databricks_yml(
            cwd, agent=agent, bundle_key=bundle_key, log=log,
        )

    # 2b. Auto-build apx-agent wheel + populate .build/
    wheel_path: Path | None = None
    if auto_build_wheel:
        wheel_path = _ensure_apx_wheel(cwd)
        if wheel_path:
            log(f"  built apx-agent wheel: {wheel_path}")
        _run_bundle_artifacts(cwd)
        log("  populated .build/")
        # If the source pyproject used the editable shape, rewrite the
        # staged copy in .build/ to use the wheel path instead. Idempotent
        # when pyproject is already wheel-pinned.
        if wheel_path:
            build_dir = cwd / ".build"
            if build_dir.is_dir():
                _rewrite_build_pyproject_for_deploy(build_dir, wheel_path)
    else:
        log("# --no-auto-build-wheel: skipping wheel build + artifacts step")

    # 2c. Auto-resolve mlflow_experiment_id if the bundle wants it and the
    # caller didn't pass one. Looks up / creates an experiment at
    # /Users/<current-user>/<bundle_name>-<target>.
    extra_vars: list[str] = []
    if auto_experiment:
        existing_vars = list(vars or ())
        already_set = any(
            v.startswith("mlflow_experiment_id=") for v in existing_vars
        )
        if not already_set:
            eid = _ensure_experiment_id(
                profile=profile,
                bundle_name=app_name,
                bundle_target=bundle_target,
                env_value=None,
            )
            if eid:
                extra_vars.append(f"mlflow_experiment_id={eid}")

    deploy_var_args: list[str] = []
    for v in list(vars or ()) + extra_vars:
        deploy_var_args.extend(["--var", v])

    # 3. databricks bundle validate
    log("# databricks bundle validate")
    validate_proc = _run_databricks_cmd(
        ["bundle", "validate", "--target", bundle_target] + deploy_var_args,
        profile=profile,
    )
    if validate_proc.returncode != 0:
        msg = (
            f"`databricks bundle validate` failed (exit {validate_proc.returncode}). "
            f"Last lines:\n{_tail_lines(validate_proc.stderr or validate_proc.stdout)}"
        )
        if json_output:
            click.echo(json.dumps({"ok": False, "error": msg, "app_name": app_name}))
            raise click.exceptions.Exit(1)
        raise click.ClickException(msg)

    # 4. databricks bundle deploy
    log("# databricks bundle deploy")
    deploy_t0 = time.monotonic()
    deploy_proc = _run_databricks_cmd(
        ["bundle", "deploy", "--target", bundle_target] + deploy_var_args,
        profile=profile,
    )
    deploy_seconds = round(time.monotonic() - deploy_t0, 2)
    if deploy_proc.returncode != 0:
        msg = (
            f"`databricks bundle deploy` failed (exit {deploy_proc.returncode}). "
            f"Last lines:\n{_tail_lines(deploy_proc.stderr or deploy_proc.stdout)}"
        )
        if json_output:
            click.echo(json.dumps({"ok": False, "error": msg, "app_name": app_name}))
            raise click.exceptions.Exit(1)
        raise click.ClickException(msg)
    log(f"  bundle deploy finished in {deploy_seconds:.1f}s")

    # 5. databricks bundle run <bundle_key>
    # bundle run takes the YAML KEY under resources.apps, which may differ
    # from the workspace app name (which is what `apps get` consumes).
    run_seconds: float | None = None
    if not no_run:
        log(f"# databricks bundle run {bundle_key}")
        run_t0 = time.monotonic()
        run_proc = _run_databricks_cmd(
            ["bundle", "run", bundle_key, "--target", bundle_target] + deploy_var_args,
            profile=profile,
        )
        run_seconds = round(time.monotonic() - run_t0, 2)
        if run_proc.returncode != 0:
            # Non-fatal — the app may already be running. Surface the tail
            # and proceed to polling so we still verify readiness.
            log(f"  bundle run returned {run_proc.returncode} (continuing)")
            log(f"  last lines:\n{_tail_lines(run_proc.stderr or run_proc.stdout)}")
        else:
            log(f"  bundle run finished in {run_seconds:.1f}s")
    else:
        log("# --no-run: skipping `databricks bundle run`")

    # 6. Poll for ACTIVE/RUNNING via `databricks apps get`
    log(f"# polling `databricks apps get {app_name}` for ACTIVE/RUNNING")
    payload = _poll_app_ready(app_name, profile, timeout_seconds=300, log=log)
    app_url = payload.get("url") or ""

    # 7. Final report
    if json_output:
        click.echo(json.dumps({
            "ok": True,
            "app_name": app_name,
            "app_url": app_url,
            "bundle_target": bundle_target,
            "deploy_seconds": deploy_seconds,
            "run_seconds": run_seconds,
        }))
    else:
        log(f"# app ready: {app_name}")
        click.echo(app_url)


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
@click.option("--target", "deploy_target",
              type=click.Choice(["model-serving", "apps"]),
              default="model-serving",
              help="Deploy target. 'model-serving' rewrites env_vars on the served "
                   "entity (existing behavior). 'apps' re-deploys the Apps bundle "
                   "with a different --var. See docs/apps-canary-hotswap-design.md.")
@click.option("--endpoint", default=None,
              help="Serving endpoint hosting the agent (model-serving only).")
@click.option("--model", "llm_arg", default=None,
              help="New model serving endpoint (e.g. databricks-claude-opus-4-7). "
                   "Required for --target model-serving.")
@click.option("--llm-endpoint", "llm_endpoint", default=None,
              help="New LLM endpoint name. Required for --target apps. "
                   "Synonymous with --model for the Apps target.")
@click.option("--no-wait", is_flag=True,
              help="Don't block until the config update completes "
                   "(model-serving only).")
@click.option("--profile", default=None,
              help="Databricks CLI profile (apps target only).")
@click.option("--bundle-target", default="prod",
              help="DAB target to redeploy (apps target only). Default 'prod'.")
@click.option("--var-name", default=None,
              help="Bundle variable name to override (apps target only). "
                   "Default 'llm_endpoint_name'.")
@click.option("--app-name", default=None,
              help="Workspace App name for the apps result record. "
                   "Defaults to the resolved app name from databricks.yml.")
def hot_swap_cmd(
    deploy_target: str,
    endpoint: str | None,
    llm_arg: str | None,
    llm_endpoint: str | None,
    no_wait: bool,
    profile: str | None,
    bundle_target: str,
    var_name: str | None,
    app_name: str | None,
) -> None:
    """Hot-swap a deployed agent's LLM endpoint.

    For --target model-serving (default): updates the
    APX_AGENT_MODEL_OVERRIDE env var on the serving endpoint so the
    next replica picks up the new model. The agent artifact is NOT
    re-logged — same model version, different LLM.

    For --target apps: re-deploys the bundle with a different
    `--var llm_endpoint_name=NEW`. The App restarts off the new env.
    See docs/apps-canary-hotswap-design.md for the rationale.

    For full artifact-version A/B with traffic split (model-serving)
    or canary soak environments (apps), use `apx canary` instead.
    """
    if deploy_target == "model-serving":
        if not endpoint:
            raise click.UsageError(
                "--endpoint is required for --target model-serving."
            )
        model_value = llm_arg or llm_endpoint
        if not model_value:
            raise click.UsageError(
                "--model is required for --target model-serving."
            )
        _hot_swap_model_serving(
            endpoint=endpoint, model=model_value, no_wait=no_wait,
        )
        return

    # --target apps
    new_value = llm_endpoint or llm_arg
    if not new_value:
        raise click.UsageError(
            "--llm-endpoint is required for --target apps."
        )
    _hot_swap_apps_cli(
        new_value=new_value,
        profile=profile,
        bundle_target=bundle_target,
        var_name=var_name,
        app_name=app_name,
    )


def _hot_swap_model_serving(
    *, endpoint: str, model: str, no_wait: bool,
) -> None:
    """Click handler body for the Model Serving hot-swap path."""
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
        click.echo("  previous override: (none — first swap on this endpoint)")
    click.echo(f"  served entities updated: {result.served_entities_updated}")
    if no_wait:
        click.echo("  (update dispatched async; pass --wait or check `databricks serving-endpoints get` to confirm)")


def _hot_swap_apps_cli(
    *,
    new_value: str,
    profile: str | None,
    bundle_target: str,
    var_name: str | None,
    app_name: str | None,
) -> None:
    """Click handler body for the Apps hot-swap path."""
    from ._hot_swap_apps import DEFAULT_LLM_VAR_NAME, hot_swap_apps

    cwd = Path.cwd()
    # Resolve the app_name from the bundle if the operator didn't pass one.
    if app_name is None:
        doc = _read_databricks_yml(cwd)
        _, app_name = _resolve_app_name(doc)

    try:
        result = hot_swap_apps(
            cwd=cwd,
            app_name=app_name,
            new_value=new_value,
            run_cmd=_run_databricks_cmd,
            profile=profile,
            bundle_target=bundle_target,
            var_name=var_name or DEFAULT_LLM_VAR_NAME,
        )
    except Exception as e:
        click.echo(f"hot-swap --target apps failed: {type(e).__name__}: {e}",
                   err=True)
        sys.exit(1)

    click.echo(f"apx hot-swap --target apps: {result.app_name}")
    click.echo(f"  bundle target:  {result.bundle_target}")
    click.echo(f"  var:            {result.var_name}")
    click.echo(f"  new value:      {result.new_value}")
    click.echo(
        f"  previous value: {result.previous_value or '(none — var had no default)'}"
    )
    click.echo(
        "  (the App is restarting; check `databricks apps get` to confirm RUNNING)"
    )


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
@click.option("--target", "deploy_target",
              type=click.Choice(["model-serving", "apps"]),
              default="model-serving",
              help="Deploy target. 'model-serving' adds a new served entity "
                   "to an existing endpoint (existing behavior). 'apps' writes "
                   "a canary DAB target and deploys a second App. See "
                   "docs/apps-canary-hotswap-design.md.")
# Model Serving args
@click.option("--endpoint", default=None,
              help="Model Serving endpoint to add the canary to "
                   "(model-serving target only).")
@click.option("--model", "registered_model_name", default=None,
              help="Three-part UC name of the registered model "
                   "(model-serving target only).")
@click.option("--version", default=None,
              help="Model version to canary (model-serving target only).")
@click.option("--workload-size", default="Small",
              help="Workload size for the new served entity (model-serving only).")
@click.option("--no-scale-to-zero", is_flag=True,
              help="Disable scale-to-zero on the new served entity (model-serving only).")
# Apps args
@click.option("--canary-version", default=None,
              help="Version label for the canary App (apps target only). "
                   "Sanitized into the DAB target name canary-<sanitized>.")
@click.option("--profile", default=None, help="Databricks CLI profile (apps target only).")
@click.option("--base-target", default="prod",
              help="Base DAB target to inherit from (apps target only). Default 'prod'.")
# Shared
@click.option("--traffic", "traffic_pct", default=10, type=int,
              help="For model-serving: percentage of traffic to route to "
                   "the new version. For apps: recorded as a hint only — "
                   "Apps has no platform-level traffic split. Default 10.")
def canary_deploy(
    deploy_target: str,
    endpoint: str | None,
    registered_model_name: str | None,
    version: str | None,
    traffic_pct: int,
    workload_size: str,
    no_scale_to_zero: bool,
    canary_version: str | None,
    profile: str | None,
    base_target: str,
) -> None:
    """Add a new model version (or App) as a canary."""
    if deploy_target == "model-serving":
        if not endpoint:
            raise click.UsageError("--endpoint is required for --target model-serving.")
        if not registered_model_name:
            raise click.UsageError("--model is required for --target model-serving.")
        if not version:
            raise click.UsageError("--version is required for --target model-serving.")
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
        return

    # --target apps
    if not canary_version:
        raise click.UsageError("--canary-version is required for --target apps.")
    from apx_agent import deploy_canary_app

    cwd = Path.cwd()
    doc = _read_databricks_yml(cwd)
    bundle_key, base_app_name = _resolve_app_name(doc)
    try:
        cfg = deploy_canary_app(
            cwd=cwd,
            bundle_key=bundle_key,
            base_app_name=base_app_name,
            canary_version=canary_version,
            traffic_hint=traffic_pct,
            run_cmd=_run_databricks_cmd,
            profile=profile,
            base_target=base_target,
        )
    except Exception as e:
        click.echo(f"canary deploy --target apps failed: {type(e).__name__}: {e}",
                   err=True)
        sys.exit(1)
    click.echo(f"Deployed canary App {cfg.canary_app_name} from version {cfg.canary_version}.")
    click.echo(f"  bundle target: {cfg.bundle_target}")
    click.echo(f"  canary URL:    {cfg.canary_app_url or '(not yet available)'}")
    click.echo(f"  traffic hint:  {cfg.traffic_hint}%  (Apps has no platform-level traffic split — "
               "route via your calling code, feature flag, or DNS)")


@canary.command("promote")
@click.option("--target", "deploy_target",
              type=click.Choice(["model-serving", "apps"]),
              default="model-serving",
              help="Deploy target. See docs/apps-canary-hotswap-design.md.")
@click.option("--endpoint", default=None,
              help="Model Serving endpoint (model-serving target only).")
@click.option("--model", "registered_model_name", default=None,
              help="Three-part UC name of the registered model (model-serving only).")
@click.option("--version", default=None,
              help="Version to send 100% of traffic to (model-serving only).")
@click.option("--canary-version", default=None,
              help="Canary version to promote (apps target only).")
@click.option("--profile", default=None, help="Databricks CLI profile (apps target only).")
@click.option("--prod-target", default="prod",
              help="Prod DAB target name (apps target only). Default 'prod'.")
@click.option("--keep-canary", is_flag=True,
              help="Don't tear down the canary App after promote (apps target only).")
def canary_promote(
    deploy_target: str,
    endpoint: str | None,
    registered_model_name: str | None,
    version: str | None,
    canary_version: str | None,
    profile: str | None,
    prod_target: str,
    keep_canary: bool,
) -> None:
    """Send 100% of traffic to a version (model-serving) or re-deploy prod
    off the canary tree (apps).
    """
    if deploy_target == "model-serving":
        if not endpoint or not registered_model_name or not version:
            raise click.UsageError(
                "--endpoint, --model, and --version are required for "
                "--target model-serving."
            )
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
        return

    # --target apps
    if not canary_version:
        raise click.UsageError("--canary-version is required for --target apps.")
    from apx_agent import promote_canary_app

    cwd = Path.cwd()
    doc = _read_databricks_yml(cwd)
    bundle_key, base_app_name = _resolve_app_name(doc)
    try:
        result = promote_canary_app(
            cwd=cwd, bundle_key=bundle_key, base_app_name=base_app_name,
            canary_version=canary_version, run_cmd=_run_databricks_cmd,
            profile=profile, prod_target=prod_target, keep_canary=keep_canary,
        )
    except Exception as e:
        click.echo(f"canary promote --target apps failed: {type(e).__name__}: {e}",
                   err=True)
        sys.exit(1)
    click.echo(f"Promoted canary {result.promoted_from_version} → prod App {result.prod_app_name}.")
    click.echo(f"  canary target removed: {result.canary_target_removed}")


@canary.command("rollback")
@click.option("--target", "deploy_target",
              type=click.Choice(["model-serving", "apps"]),
              default="model-serving",
              help="Deploy target. See docs/apps-canary-hotswap-design.md.")
@click.option("--endpoint", default=None,
              help="Model Serving endpoint (model-serving target only).")
@click.option("--model", "registered_model_name", default=None,
              help="Three-part UC name of the registered model (model-serving only).")
@click.option("--version", default=None,
              help="Version to roll back to (model-serving only).")
@click.option("--canary-version", default=None,
              help="Prior version label to roll prod back to (apps target only).")
@click.option("--profile", default=None, help="Databricks CLI profile (apps target only).")
@click.option("--prod-target", default="prod",
              help="Prod DAB target name (apps target only). Default 'prod'.")
def canary_rollback(
    deploy_target: str,
    endpoint: str | None,
    registered_model_name: str | None,
    version: str | None,
    canary_version: str | None,
    profile: str | None,
    prod_target: str,
) -> None:
    """Roll back to a prior version. Functionally equivalent to promote."""
    if deploy_target == "model-serving":
        if not endpoint or not registered_model_name or not version:
            raise click.UsageError(
                "--endpoint, --model, and --version are required for "
                "--target model-serving."
            )
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
        return

    # --target apps
    if not canary_version:
        raise click.UsageError("--canary-version is required for --target apps.")
    from apx_agent import rollback_canary_app

    cwd = Path.cwd()
    doc = _read_databricks_yml(cwd)
    bundle_key, base_app_name = _resolve_app_name(doc)
    try:
        result = rollback_canary_app(
            cwd=cwd, bundle_key=bundle_key, base_app_name=base_app_name,
            canary_version=canary_version, run_cmd=_run_databricks_cmd,
            profile=profile, prod_target=prod_target,
        )
    except Exception as e:
        click.echo(f"canary rollback --target apps failed: {type(e).__name__}: {e}",
                   err=True)
        sys.exit(1)
    click.echo(f"Rolled back prod App {result.prod_app_name} to canary tree {result.promoted_from_version}.")
    click.echo(f"  canary target removed: {result.canary_target_removed}")


@canary.command("analyze")
@click.option("--target", "deploy_target",
              type=click.Choice(["model-serving", "apps"]),
              default="model-serving",
              help="Deploy target. See docs/apps-canary-hotswap-design.md.")
@click.option("--endpoint", default=None,
              help="Model Serving endpoint (model-serving target only).")
@click.option("--canary-version", default=None,
              help="Canary version to compare against prod (apps target only). "
                   "Used to derive the canary App name.")
@click.option("--experiment", default=None,
              help="MLflow experiment to read traces from. Falls back to "
                   "[tool.apx.agent].experiment in pyproject.toml.")
@click.option("--hours", default=24, type=int, help="Lookback window. Default 24h.")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def canary_analyze(
    deploy_target: str,
    endpoint: str | None,
    canary_version: str | None,
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

    if deploy_target == "model-serving":
        if not endpoint:
            raise click.UsageError("--endpoint is required for --target model-serving.")
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
        return

    # --target apps
    if not canary_version:
        raise click.UsageError("--canary-version is required for --target apps.")
    from apx_agent import analyze_canary_app, canary_app_name

    cwd = Path.cwd()
    doc = _read_databricks_yml(cwd)
    _, base_app_name = _resolve_app_name(doc)
    canary_full_name = canary_app_name(base_app_name, canary_version)

    apps_report = analyze_canary_app(
        prod_app_name=base_app_name,
        canary_app_name=canary_full_name,
        experiment=effective_experiment,
        lookback_hours=hours,
    )

    if fmt == "json":
        click.echo(json.dumps({
            "prod_app_name": apps_report.prod_app_name,
            "canary_app_name": apps_report.canary_app_name,
            "lookback_hours": apps_report.lookback_hours,
            "apps": [
                {
                    "app_name": a.app_name,
                    "requests": a.requests,
                    "errors": a.errors,
                    "error_rate": a.error_rate,
                    "latency_p50_ms": a.latency_p50_ms,
                    "latency_p95_ms": a.latency_p95_ms,
                    "latency_avg_ms": a.latency_avg_ms,
                }
                for a in apps_report.apps
            ],
        }, indent=2, default=str))
        return

    click.echo(
        f"# canary analysis (apps): prod={apps_report.prod_app_name} "
        f"canary={apps_report.canary_app_name} "
        f"(last {apps_report.lookback_hours}h)"
    )
    total_requests = sum(a.requests for a in apps_report.apps)
    if total_requests == 0:
        click.echo(
            "No traces matched either App. Either no traffic in the window "
            f"or the `{ 'apx.app.name' }` tag isn't on emitted traces — "
            "fall back to `databricks apps logs <name>` for unstructured logs."
        )
        return
    click.echo(f"{'APP':<40}  {'REQUESTS':>9}  {'ERRORS':>7}  "
               f"{'ERR %':>6}  {'P50 ms':>7}  {'P95 ms':>7}  {'AVG ms':>7}")
    for a in apps_report.apps:
        err_pct = f"{a.error_rate * 100:.1f}" if a.requests else "-"
        avg = f"{a.latency_avg_ms:.0f}" if a.latency_avg_ms is not None else "-"
        p50 = str(a.latency_p50_ms) if a.latency_p50_ms is not None else "-"
        p95 = str(a.latency_p95_ms) if a.latency_p95_ms is not None else "-"
        click.echo(f"{a.app_name:<40}  {a.requests:>9}  {a.errors:>7}  "
                   f"{err_pct:>6}  {p50:>7}  {p95:>7}  {avg:>7}")
    best_latency = apps_report.best_by_latency()
    best_errors = apps_report.best_by_error_rate()
    if best_latency is not None:
        click.echo(f"\nBest P95 latency: {best_latency.app_name} ({best_latency.latency_p95_ms} ms)")
    if best_errors is not None:
        click.echo(f"Best error rate: {best_errors.app_name} ({best_errors.error_rate * 100:.2f}%)")


# ---------------------------------------------------------------------------
# watchdog — read-side compliance posture inspection
# ---------------------------------------------------------------------------


_ENV_VIOLATIONS_TABLE = "APX_WATCHDOG_VIOLATIONS_TABLE"
_ENV_MCP_URL = "APX_WATCHDOG_MCP_URL"
_ENV_MCP_TOOL = "APX_WATCHDOG_MCP_TOOL_NAME"


@main.group()
def watchdog() -> None:
    """Inspect databricks-watchdog compliance posture from the CLI.

    Reads the UC violations table and watchdog's MCP tools without
    needing to load the agent. Configure once via env vars:

      APX_WATCHDOG_VIOLATIONS_TABLE=catalog.schema.violations
      APX_WATCHDOG_MCP_URL=https://watchdog.example.com/mcp
      APX_WATCHDOG_MCP_TOOL_NAME=evaluate_operation
    """


@watchdog.command("violations")
@click.option("--table", "violations_table", default=None,
              help=f"Three-part UC name of the watchdog violations table. "
                   f"Falls back to ${_ENV_VIOLATIONS_TABLE}.")
@click.option("--agent", "agent_name", default=None,
              help="Filter to violations for this agent_name.")
@click.option("--hours", default=24, type=int, help="Lookback window. Default 24h.")
@click.option("--limit", default=50, type=int, help="Max rows. Default 50.")
@click.option("--warehouse-id", default=None, help="SQL warehouse for the read.")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def watchdog_violations(
    violations_table: str | None,
    agent_name: str | None,
    hours: int,
    limit: int,
    warehouse_id: str | None,
    fmt: str,
) -> None:
    """Recent reject / redact decisions reported by WatchdogGuard."""
    import os

    table = violations_table or os.environ.get(_ENV_VIOLATIONS_TABLE)
    if not table:
        raise click.UsageError(
            f"Pass --table catalog.schema.table or set {_ENV_VIOLATIONS_TABLE}."
        )
    if table.count(".") != 2:
        raise click.UsageError(
            f"--table must be a three-part UC name; got {table!r}"
        )

    from databricks.sdk import WorkspaceClient

    from apx_agent import run_sql

    where_parts: list[str] = [
        f"ts > CURRENT_TIMESTAMP - INTERVAL {hours} HOUR",
    ]
    if agent_name:
        # _sql_str_literal isn't exported; inline the escape rules.
        escaped = agent_name.replace("'", "''")
        where_parts.append(f"agent_name = '{escaped}'")
    sql = (
        f"SELECT ts, agent_name, operation, action, reason, "
        f"  policy_id, domain, context, metadata "
        f"FROM {table} "
        f"WHERE {' AND '.join(where_parts)} "
        f"ORDER BY ts DESC "
        f"LIMIT {limit}"
    )

    try:
        rows = run_sql(WorkspaceClient(), sql, warehouse_id=warehouse_id)
    except Exception as e:
        raise click.ClickException(f"Failed to read violations: {e}") from e

    if fmt == "json":
        click.echo(json.dumps(rows, indent=2, default=str))
        return

    click.echo(f"# violations on {table} (last {hours}h"
               + (f", agent={agent_name}" if agent_name else "")
               + f", limit {limit})")
    if not rows:
        click.echo("No violations matched.")
        return
    click.echo(f"{'TS':<24}  {'AGENT':<20}  {'OP':<14}  "
               f"{'ACTION':<8}  {'POLICY':<24}  REASON")
    for r in rows:
        ts = str(r.get("ts") or "")[:23]
        click.echo(
            f"{ts:<24}  "
            f"{(r.get('agent_name') or '-'):<20}  "
            f"{(r.get('operation') or '-'):<14}  "
            f"{(r.get('action') or '-'):<8}  "
            f"{(r.get('policy_id') or '-'):<24}  "
            f"{(r.get('reason') or '-')}"
        )


@watchdog.command("status")
@click.option("--agent", "agent_name", default=None,
              help="Agent name to query posture for. Optional; some watchdog "
                   "tools return workspace-wide status when omitted.")
@click.option("--mcp-url", default=None,
              help=f"Watchdog MCP endpoint URL. Falls back to ${_ENV_MCP_URL}.")
@click.option("--mcp-tool", "mcp_tool_name", default=None,
              help=f"MCP tool name to invoke. Falls back to ${_ENV_MCP_TOOL}.")
@click.option("--timeout", "timeout_seconds", default=5.0, type=float,
              help="MCP call timeout. Default 5s.")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def watchdog_status(
    agent_name: str | None,
    mcp_url: str | None,
    mcp_tool_name: str | None,
    timeout_seconds: float,
    fmt: str,
) -> None:
    """Query a watchdog MCP tool for the agent's compliance posture."""
    import os

    url = mcp_url or os.environ.get(_ENV_MCP_URL)
    tool_name = mcp_tool_name or os.environ.get(_ENV_MCP_TOOL)
    if not url:
        raise click.UsageError(
            f"Pass --mcp-url or set {_ENV_MCP_URL}."
        )
    if not tool_name:
        raise click.UsageError(
            f"Pass --mcp-tool or set {_ENV_MCP_TOOL}."
        )

    from apx_agent import WatchdogClient, make_mcp_transport

    transport = make_mcp_transport(
        url, tool_name=tool_name, timeout_seconds=timeout_seconds,
    )
    client = WatchdogClient(transport=transport)
    decision = client.evaluate(
        operation="status",
        context={"agent_name": agent_name} if agent_name else {},
    )

    payload = {
        "action": decision.action,
        "reason": decision.reason,
        "policy_id": decision.policy_id,
        "domain": decision.domain,
        "metadata": decision.metadata,
    }

    if fmt == "json":
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    click.echo(f"# watchdog status"
               + (f" for agent={agent_name}" if agent_name else "")
               + f" via {tool_name}")
    click.echo(f"  action:     {decision.action}")
    if decision.reason:
        click.echo(f"  reason:     {decision.reason}")
    if decision.policy_id:
        click.echo(f"  policy_id:  {decision.policy_id}")
    if decision.domain:
        click.echo(f"  domain:     {decision.domain}")
    if decision.metadata:
        click.echo(f"  metadata:   {json.dumps(decision.metadata, default=str)}")


# ---------------------------------------------------------------------------
# memory / examples — store CRUD + recall from the CLI
# ---------------------------------------------------------------------------
#
# Both subcommand groups (`apx memory ...` and `apx examples ...`) operate
# against a user-supplied store. The store comes from one of two places:
#
#   1. --store-module MODULE:VAR              (explicit flag)
#   2. [tool.apx.agent].memory_store /         (pyproject fallback)
#      [tool.apx.agent].example_store
#
# The referenced variable must be an importable :class:`MemoryStore` or
# :class:`ExampleStore` instance. The CLI is store-shape-agnostic — anything
# that conforms to the protocol works (InMemory, Lakebase, custom).
#
# JSON is the default output format; pass --format text for a markdown-style
# rendering useful at a terminal.


def _load_store(
    module_spec: str,
    *,
    store_kind: str,  # "memory" or "example", used for the pyproject key
) -> Any:
    """Load a MemoryStore / ExampleStore instance from MODULE:VAR.

    Resolution order:
      1. ``module_spec`` argument (the --store-module flag value).
      2. ``[tool.apx.agent].{store_kind}_store`` in pyproject.toml.

    Raises ``click.UsageError`` if no spec is configured anywhere, and
    ``click.ClickException`` on a load failure.
    """
    spec = module_spec
    if not spec:
        cfg_key = f"{store_kind}_store"
        spec = _read_apx_agent_config().get(cfg_key)
    if not spec:
        raise click.UsageError(
            f"Pass --store-module MODULE:VAR or set "
            f"[tool.apx.agent].{store_kind}_store in pyproject.toml. "
            f"The variable must be a {store_kind.title()}Store instance."
        )

    module_path, variable = _parse_module_spec(spec)
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise click.ClickException(
            f"Failed to import store module {module_path!r}: {e}. "
            f"Make sure the module is on PYTHONPATH or in the current directory."
        ) from e
    if not hasattr(module, variable):
        raise click.ClickException(
            f"Module {module_path!r} has no attribute {variable!r}."
        )
    return getattr(module, variable)


def _parse_tags(tags_csv: str | None) -> tuple[str, ...] | None:
    """Parse a comma-separated tag list. Empty / None -> None.

    Whitespace around individual tags is trimmed; empty segments are
    dropped. Returns ``None`` (the any-tag wildcard) when nothing parses.
    """
    if not tags_csv:
        return None
    parts = tuple(t.strip() for t in tags_csv.split(",") if t.strip())
    return parts or None


def _memory_to_dict(m: Any) -> dict[str, Any]:
    """Render a :class:`Memory` to a JSON-safe dict (drops the embedding)."""
    return {
        "id": m.id,
        "principal_id": m.principal_id,
        "namespace": m.namespace,
        "content": m.content,
        "tags": list(m.tags),
        "importance": m.importance,
        "metadata": dict(m.metadata),
        "created_at": m.created_at,
        "updated_at": m.updated_at,
    }


def _example_to_dict(e: Any) -> dict[str, Any]:
    """Render an :class:`Example` to a JSON-safe dict (drops the embedding)."""
    return {
        "id": e.id,
        "agent_id": e.agent_id,
        "intent": e.intent,
        "input": e.input,
        "output": e.output,
        "score": e.score,
        "tags": list(e.tags),
        "metadata": dict(e.metadata),
        "created_at": e.created_at,
        "updated_at": e.updated_at,
    }


def _emit(payload: Any, fmt: str, *, text_fn: Any = None) -> None:
    """Emit ``payload`` as JSON or as text (via the provided ``text_fn``)."""
    if fmt == "json":
        click.echo(json.dumps(payload, indent=2, default=str))
        return
    if text_fn is None:
        # No bespoke text renderer; fall through to JSON-ish.
        click.echo(json.dumps(payload, indent=2, default=str))
        return
    text_fn(payload)


# --- memory group ----------------------------------------------------------


@main.group()
def memory() -> None:
    """Operate on a MemoryStore — recall, remember, forget, list.

    Configure the store via ``--store-module MODULE:VAR`` on each command,
    or set ``[tool.apx.agent].memory_store = "module:variable"`` in
    ``pyproject.toml`` to share one default across calls.

    The referenced variable must be an instance of
    :class:`apx_agent.MemoryStore` — typically
    :class:`apx_agent.InMemoryMemoryStore` for local dev or
    :class:`apx_agent.LakebaseMemoryStore` for shared deployments.
    """


@memory.command("recall")
@click.option("--principal-id", required=True, help="Scopes recall to one principal.")
@click.option("--query", required=True, help="Natural-language query.")
@click.option("--namespace", default=None, help="Optional namespace filter.")
@click.option("--tags", "tags_csv", default=None,
              help="Comma-separated tag list (any-of filter).")
@click.option("-k", "top_k", default=5, type=int, help="Top-k. Default 5.")
@click.option("--store-module", "store_module", default=None,
              help="MODULE:VAR pointing at a MemoryStore instance. "
                   "Falls back to [tool.apx.agent].memory_store.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]),
              default="json", help="Output format. Default: json.")
def memory_recall_cmd(
    principal_id: str,
    query: str,
    namespace: str | None,
    tags_csv: str | None,
    top_k: int,
    store_module: str | None,
    fmt: str,
) -> None:
    """Recall top-k memories matching QUERY for PRINCIPAL_ID."""
    from ._memory import RecallOptions

    store = _load_store(store_module, store_kind="memory")
    results = store.recall(
        RecallOptions(
            principal_id=principal_id,
            query=query,
            namespace=namespace,
            tags=_parse_tags(tags_csv),
            k=top_k,
        )
    )
    payload = [
        {"score": r.score, "memory": _memory_to_dict(r.memory)}
        for r in results
    ]

    def _text(rows: list[dict[str, Any]]) -> None:
        if not rows:
            click.echo(f"# memory recall for principal={principal_id!r}: no hits.")
            return
        click.echo(f"# memory recall for principal={principal_id!r} (top {len(rows)})")
        for r in rows:
            mem = r["memory"]
            click.echo(f"- [{r['score']:.3f}] ({mem['id']}) {mem['content']}")

    _emit(payload, fmt, text_fn=_text)


@memory.command("remember")
@click.option("--principal-id", required=True, help="Scopes the memory to a principal.")
@click.option("--content", required=True, help="The memory text to remember.")
@click.option("--namespace", default=None, help="Optional namespace (defaults to 'default').")
@click.option("--tags", "tags_csv", default=None,
              help="Comma-separated tag list.")
@click.option("--importance", default=0.5, type=float,
              help="Importance 0..1. Default 0.5.")
@click.option("--store-module", "store_module", default=None,
              help="MODULE:VAR pointing at a MemoryStore instance.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]),
              default="json", help="Output format. Default: json.")
def memory_remember_cmd(
    principal_id: str,
    content: str,
    namespace: str | None,
    tags_csv: str | None,
    importance: float,
    store_module: str | None,
    fmt: str,
) -> None:
    """Insert a memory into the store."""
    store = _load_store(store_module, store_kind="memory")
    payload_in: dict[str, Any] = {
        "principal_id": principal_id,
        "content": content,
        "importance": importance,
    }
    if namespace:
        payload_in["namespace"] = namespace
    tags = _parse_tags(tags_csv)
    if tags is not None:
        payload_in["tags"] = list(tags)

    materialized = store.add(payload_in)
    payload = _memory_to_dict(materialized)

    def _text(_: dict[str, Any]) -> None:
        click.echo(f"# remembered  id={materialized.id}")

    _emit(payload, fmt, text_fn=_text)


@memory.command("forget")
@click.option("--id", "memory_id", required=True, help="Memory id to delete.")
@click.option("--store-module", "store_module", default=None,
              help="MODULE:VAR pointing at a MemoryStore instance.")
def memory_forget_cmd(memory_id: str, store_module: str | None) -> None:
    """Delete a memory by id. Exits non-zero if no row matched."""
    store = _load_store(store_module, store_kind="memory")
    ok = store.delete(memory_id)
    if not ok:
        click.echo(f"# no memory with id {memory_id!r}", err=True)
        sys.exit(1)
    click.echo(json.dumps({"deleted": memory_id}))


@memory.command("list")
@click.option("--principal-id", required=True, help="Scopes the listing to one principal.")
@click.option("--namespace", default=None, help="Optional namespace filter.")
@click.option("--tags", "tags_csv", default=None, help="Comma-separated tag list.")
@click.option("--limit", default=100, type=int, help="Max rows. Default 100.")
@click.option("--store-module", "store_module", default=None,
              help="MODULE:VAR pointing at a MemoryStore instance.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]),
              default="json", help="Output format. Default: json.")
def memory_list_cmd(
    principal_id: str,
    namespace: str | None,
    tags_csv: str | None,
    limit: int,
    store_module: str | None,
    fmt: str,
) -> None:
    """List memories for PRINCIPAL_ID."""
    from ._memory import MemoryFilter

    store = _load_store(store_module, store_kind="memory")
    rows = store.list(
        MemoryFilter(
            principal_id=principal_id,
            namespace=namespace,
            tags=_parse_tags(tags_csv),
            limit=limit,
        )
    )
    payload = [_memory_to_dict(m) for m in rows]

    def _text(rows: list[dict[str, Any]]) -> None:
        if not rows:
            click.echo(f"# memory list for principal={principal_id!r}: empty.")
            return
        click.echo(f"# memory list for principal={principal_id!r} ({len(rows)} rows)")
        for m in rows:
            click.echo(f"- ({m['id']}) ns={m['namespace']} tags={m['tags']} "
                       f"importance={m['importance']:.2f}  {m['content']}")

    _emit(payload, fmt, text_fn=_text)


# --- examples group --------------------------------------------------------


@main.group()
def examples() -> None:
    """Operate on an ExampleStore — find, save, remove, list.

    Configure the store via ``--store-module MODULE:VAR`` on each command,
    or set ``[tool.apx.agent].example_store = "module:variable"`` in
    ``pyproject.toml`` to share one default across calls.

    The referenced variable must be an instance of
    :class:`apx_agent.ExampleStore` — typically
    :class:`apx_agent.InMemoryExampleStore` for local dev or
    :class:`apx_agent.LakebaseExampleStore` for shared deployments.
    """


@examples.command("find")
@click.option("--agent-id", required=True, help="Scopes the search to one agent.")
@click.option("--query", required=True, help="Natural-language query.")
@click.option("--intent", default=None, help="Optional intent filter.")
@click.option("--tags", "tags_csv", default=None, help="Comma-separated tag list.")
@click.option("-k", "top_k", default=5, type=int, help="Top-k. Default 5.")
@click.option("--store-module", "store_module", default=None,
              help="MODULE:VAR pointing at an ExampleStore instance.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]),
              default="json", help="Output format. Default: json.")
def examples_find_cmd(
    agent_id: str,
    query: str,
    intent: str | None,
    tags_csv: str | None,
    top_k: int,
    store_module: str | None,
    fmt: str,
) -> None:
    """Find top-k similar examples for QUERY."""
    from ._example import FindSimilarOptions

    store = _load_store(store_module, store_kind="example")
    results = store.find_similar(
        FindSimilarOptions(
            agent_id=agent_id,
            query=query,
            intent=intent,
            tags=_parse_tags(tags_csv),
            k=top_k,
        )
    )
    payload = [
        {"score": r.score, "example": _example_to_dict(r.example)}
        for r in results
    ]

    def _text(rows: list[dict[str, Any]]) -> None:
        if not rows:
            click.echo(f"# examples find for agent={agent_id!r}: no hits.")
            return
        click.echo(f"# examples find for agent={agent_id!r} (top {len(rows)})")
        for r in rows:
            ex = r["example"]
            click.echo(f"- [{r['score']:.3f}] ({ex['id']}) intent={ex['intent']}")
            click.echo(f"    in:  {ex['input']}")
            click.echo(f"    out: {ex['output']}")

    _emit(payload, fmt, text_fn=_text)


@examples.command("save")
@click.option("--agent-id", required=True, help="Scopes the example to an agent.")
@click.option("--input", "ex_input", required=True, help="The example input.")
@click.option("--output", "ex_output", required=True, help="The example output.")
@click.option("--intent", default=None, help="Optional intent bucket.")
@click.option("--score", default=None, type=float, help="Optional 0..1 quality score.")
@click.option("--tags", "tags_csv", default=None, help="Comma-separated tag list.")
@click.option("--store-module", "store_module", default=None,
              help="MODULE:VAR pointing at an ExampleStore instance.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]),
              default="json", help="Output format. Default: json.")
def examples_save_cmd(
    agent_id: str,
    ex_input: str,
    ex_output: str,
    intent: str | None,
    score: float | None,
    tags_csv: str | None,
    store_module: str | None,
    fmt: str,
) -> None:
    """Insert an example into the store."""
    store = _load_store(store_module, store_kind="example")
    payload_in: dict[str, Any] = {
        "agent_id": agent_id,
        "input": ex_input,
        "output": ex_output,
    }
    if intent:
        payload_in["intent"] = intent
    if score is not None:
        payload_in["score"] = score
    tags = _parse_tags(tags_csv)
    if tags is not None:
        payload_in["tags"] = list(tags)

    materialized = store.add(payload_in)
    payload = _example_to_dict(materialized)

    def _text(_: dict[str, Any]) -> None:
        click.echo(f"# saved  id={materialized.id}")

    _emit(payload, fmt, text_fn=_text)


@examples.command("remove")
@click.option("--id", "example_id", required=True, help="Example id to delete.")
@click.option("--store-module", "store_module", default=None,
              help="MODULE:VAR pointing at an ExampleStore instance.")
def examples_remove_cmd(example_id: str, store_module: str | None) -> None:
    """Delete an example by id. Exits non-zero if no row matched."""
    store = _load_store(store_module, store_kind="example")
    ok = store.delete(example_id)
    if not ok:
        click.echo(f"# no example with id {example_id!r}", err=True)
        sys.exit(1)
    click.echo(json.dumps({"deleted": example_id}))


@examples.command("list")
@click.option("--agent-id", required=True, help="Scopes the listing to one agent.")
@click.option("--intent", default=None, help="Optional intent filter.")
@click.option("--tags", "tags_csv", default=None, help="Comma-separated tag list.")
@click.option("--limit", default=100, type=int, help="Max rows. Default 100.")
@click.option("--store-module", "store_module", default=None,
              help="MODULE:VAR pointing at an ExampleStore instance.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]),
              default="json", help="Output format. Default: json.")
def examples_list_cmd(
    agent_id: str,
    intent: str | None,
    tags_csv: str | None,
    limit: int,
    store_module: str | None,
    fmt: str,
) -> None:
    """List examples for AGENT_ID."""
    from ._example import ExampleFilter

    store = _load_store(store_module, store_kind="example")
    rows = store.list(
        ExampleFilter(
            agent_id=agent_id,
            intent=intent,
            tags=_parse_tags(tags_csv),
            limit=limit,
        )
    )
    payload = [_example_to_dict(e) for e in rows]

    def _text(rows: list[dict[str, Any]]) -> None:
        if not rows:
            click.echo(f"# examples list for agent={agent_id!r}: empty.")
            return
        click.echo(f"# examples list for agent={agent_id!r} ({len(rows)} rows)")
        for ex in rows:
            score = f"{ex['score']:.2f}" if ex["score"] is not None else "-"
            click.echo(f"- ({ex['id']}) intent={ex['intent']} score={score} "
                       f"tags={ex['tags']}")
            click.echo(f"    in:  {ex['input']}")
            click.echo(f"    out: {ex['output']}")

    _emit(payload, fmt, text_fn=_text)


# ---------------------------------------------------------------------------
# examples mine / memory consolidate — wrap mine_examples / consolidate_memories
# ---------------------------------------------------------------------------
#
# Both subcommands wrap library calls that take injectable stores + callables.
# We resolve each MODULE:VAR via :func:`_load_callable` (parallel to
# :func:`_load_store` but for arbitrary callables) and pass the resolved
# objects straight through to the library function.
#
# Pyproject fallback keys (under ``[tool.apx.agent]``):
#
#   * ``session_store``   — examples mine, --session-store
#   * ``example_store``   — examples mine, --example-store (shared w/ examples find)
#   * ``intent_fn``       — examples mine, --intent-fn
#   * ``score_fn``        — examples mine, --score-fn
#   * ``filter_fn``       — examples mine, --filter-fn
#   * ``tags_fn``         — examples mine, --tags-fn
#   * ``metadata_fn``     — examples mine, --metadata-fn
#   * ``memory_store``    — memory consolidate, --store (shared w/ memory list/...)
#   * ``summarize_fn``    — memory consolidate, --summarize-fn


def _load_callable(
    module_spec: str | None,
    *,
    pyproject_key: str | None = None,
    required: bool = False,
    label: str = "callable",
) -> Any:
    """Load an arbitrary callable from MODULE:VAR.

    Mirrors :func:`_load_store` but generalized: there is no single
    canonical pyproject key for callables — the caller passes one in
    via ``pyproject_key`` so each ``--*-fn`` flag falls back to its own
    section key. Returns ``None`` when nothing is configured and
    ``required=False``.
    """
    spec = module_spec
    if not spec and pyproject_key:
        spec = _read_apx_agent_config().get(pyproject_key)
    if not spec:
        if required:
            raise click.UsageError(
                f"Pass the {label} MODULE:VAR flag or set "
                f"[tool.apx.agent].{pyproject_key} in pyproject.toml."
            )
        return None

    module_path, variable = _parse_module_spec(spec)
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise click.ClickException(
            f"Failed to import {label} module {module_path!r}: {e}. "
            f"Make sure the module is on PYTHONPATH or in the current "
            f"directory."
        ) from e
    if not hasattr(module, variable):
        raise click.ClickException(
            f"Module {module_path!r} has no attribute {variable!r}."
        )
    return getattr(module, variable)


def _load_store_spec(
    module_spec: str | None,
    *,
    pyproject_key: str,
    label: str,
) -> Any:
    """Like :func:`_load_store` but with a caller-chosen pyproject key.

    The legacy ``_load_store`` builds the key from ``store_kind`` —
    ``memory_store`` / ``example_store``. Mining + consolidation need
    distinct keys (``session_store``, ``example_store``, ``memory_store``)
    so we expose the key explicitly here.
    """
    spec = module_spec
    if not spec:
        spec = _read_apx_agent_config().get(pyproject_key)
    if not spec:
        raise click.UsageError(
            f"Pass the {label} MODULE:VAR flag or set "
            f"[tool.apx.agent].{pyproject_key} in pyproject.toml."
        )

    module_path, variable = _parse_module_spec(spec)
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise click.ClickException(
            f"Failed to import {label} module {module_path!r}: {e}. "
            f"Make sure the module is on PYTHONPATH or in the current "
            f"directory."
        ) from e
    if not hasattr(module, variable):
        raise click.ClickException(
            f"Module {module_path!r} has no attribute {variable!r}."
        )
    return getattr(module, variable)


@examples.command("mine")
@click.option("--session-store", "session_store_spec", default=None,
              help="MODULE:VAR pointing at a SessionStore instance. "
                   "Falls back to [tool.apx.agent].session_store.")
@click.option("--example-store", "example_store_spec", default=None,
              help="MODULE:VAR pointing at an ExampleStore instance. "
                   "Falls back to [tool.apx.agent].example_store.")
@click.option("--agent-id", required=True,
              help="Agent id stamped on every mined Example.")
@click.option("--session-ids", "session_ids_csv", default=None,
              help="Comma-separated session ids to mine. "
                   "Default: every session in the store.")
@click.option("--intent-fn", "intent_fn_spec", default=None,
              help="MODULE:VAR of a Turn -> str intent classifier.")
@click.option("--score-fn", "score_fn_spec", default=None,
              help="MODULE:VAR of a Turn -> float|None scorer.")
@click.option("--filter-fn", "filter_fn_spec", default=None,
              help="MODULE:VAR of a Turn -> bool include filter.")
@click.option("--tags-fn", "tags_fn_spec", default=None,
              help="MODULE:VAR of a Turn -> Sequence[str] tag extractor.")
@click.option("--metadata-fn", "metadata_fn_spec", default=None,
              help="MODULE:VAR of a Turn -> Mapping[str, Any] metadata fn.")
@click.option("--limit", default=None, type=int,
              help="Max examples to write. Default: no limit.")
@click.option("--min-score", default=None, type=float,
              help="Discard turns whose --score-fn returned <min-score.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Compute Examples client-side without writing.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]),
              default="json", help="Output format. Default: json.")
def examples_mine_cmd(
    session_store_spec: str | None,
    example_store_spec: str | None,
    agent_id: str,
    session_ids_csv: str | None,
    intent_fn_spec: str | None,
    score_fn_spec: str | None,
    filter_fn_spec: str | None,
    tags_fn_spec: str | None,
    metadata_fn_spec: str | None,
    limit: int | None,
    min_score: float | None,
    dry_run: bool,
    fmt: str,
) -> None:
    """Mine (user, assistant) Examples from a SessionStore's history."""
    from ._example_mining import mine_examples

    session_store = _load_store_spec(
        session_store_spec,
        pyproject_key="session_store",
        label="--session-store",
    )
    example_store = _load_store_spec(
        example_store_spec,
        pyproject_key="example_store",
        label="--example-store",
    )

    intent_fn = _load_callable(
        intent_fn_spec, pyproject_key="intent_fn", label="--intent-fn",
    )
    score_fn = _load_callable(
        score_fn_spec, pyproject_key="score_fn", label="--score-fn",
    )
    filter_fn = _load_callable(
        filter_fn_spec, pyproject_key="filter_fn", label="--filter-fn",
    )
    tags_fn = _load_callable(
        tags_fn_spec, pyproject_key="tags_fn", label="--tags-fn",
    )
    metadata_fn = _load_callable(
        metadata_fn_spec, pyproject_key="metadata_fn", label="--metadata-fn",
    )

    session_ids: list[str] | None = None
    if session_ids_csv:
        session_ids = [s.strip() for s in session_ids_csv.split(",") if s.strip()]

    result = mine_examples(
        session_store=session_store,
        example_store=example_store,
        agent_id=agent_id,
        session_ids=session_ids,
        intent_fn=intent_fn,
        score_fn=score_fn,
        filter_fn=filter_fn,
        tags_fn=tags_fn,
        metadata_fn=metadata_fn,
        limit=limit,
        min_score=min_score,
        dry_run=dry_run,
    )

    if fmt == "text":
        click.echo(
            f"Mined {len(result.examples)} examples from "
            f"{result.sessions_scanned} sessions "
            f"({result.turns_considered} turns considered)"
        )
        return

    payload = {
        "sessions_scanned": result.sessions_scanned,
        "turns_considered": result.turns_considered,
        "examples_added": result.examples_added,
        "dry_run": dry_run,
        "examples": [_example_to_dict(e) for e in result.examples],
    }
    click.echo(json.dumps(payload, indent=2, default=str))


@memory.command("consolidate")
@click.option("--store", "store_spec", default=None,
              help="MODULE:VAR pointing at a MemoryStore instance. "
                   "Falls back to [tool.apx.agent].memory_store.")
@click.option("--principal-id", required=True,
              help="Scope consolidation to a single principal.")
@click.option("--summarize-fn", "summarize_fn_spec", default=None,
              help="MODULE:VAR of a Sequence[Memory] -> str summarizer. "
                   "Falls back to [tool.apx.agent].summarize_fn.")
@click.option("--namespace", default=None,
              help="Optional namespace filter for the candidate pool.")
@click.option("--max-age-seconds", default=None, type=float,
              help="Only consolidate memories older than this many seconds.")
@click.option("--min-importance", default=None, type=float,
              help="Importance floor for candidate selection.")
@click.option("--keep-originals", is_flag=True, default=False,
              help="Skip deletion of the source memories after write.")
@click.option("--consolidated-namespace", default="consolidated",
              help="Namespace for the consolidated row. Default: consolidated.")
@click.option("--consolidated-tags", "consolidated_tags_csv", default=None,
              help="Comma-separated tag list. Default: consolidated.")
@click.option("--consolidated-importance", default=0.7, type=float,
              help="Importance for the consolidated row. Default 0.7.")
@click.option("--min-memories-for-consolidation", default=5, type=int,
              help="Skip when fewer than this many candidates match. "
                   "Default 5.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Materialize the summary without writing or deleting.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]),
              default="json", help="Output format. Default: json.")
def memory_consolidate_cmd(
    store_spec: str | None,
    principal_id: str,
    summarize_fn_spec: str | None,
    namespace: str | None,
    max_age_seconds: float | None,
    min_importance: float | None,
    keep_originals: bool,
    consolidated_namespace: str,
    consolidated_tags_csv: str | None,
    consolidated_importance: float,
    min_memories_for_consolidation: int,
    dry_run: bool,
    fmt: str,
) -> None:
    """Summarize older memories into a single consolidated row."""
    from ._memory_consolidate import consolidate_memories

    store = _load_store_spec(
        store_spec, pyproject_key="memory_store", label="--store",
    )
    summarize_fn = _load_callable(
        summarize_fn_spec,
        pyproject_key="summarize_fn",
        label="--summarize-fn",
        required=True,
    )

    consolidated_tags = _parse_tags(consolidated_tags_csv) or ("consolidated",)

    result = consolidate_memories(
        store=store,
        principal_id=principal_id,
        summarize_fn=summarize_fn,
        namespace=namespace,
        max_age_seconds=max_age_seconds,
        min_importance=min_importance,
        keep_originals=keep_originals,
        consolidated_namespace=consolidated_namespace,
        consolidated_tags=consolidated_tags,
        consolidated_importance=consolidated_importance,
        min_memories_for_consolidation=min_memories_for_consolidation,
        dry_run=dry_run,
    )

    if result.consolidated_memory is None:
        # Below threshold — surface a clear error + non-zero exit.
        msg = (
            f"# only {result.candidates_found} candidate memories "
            f"(need >= {min_memories_for_consolidation})"
        )
        if fmt == "text":
            click.echo(msg, err=True)
        else:
            click.echo(json.dumps({
                "candidates_found": result.candidates_found,
                "consolidated_memory": None,
                "deleted_ids": [],
                "dry_run": dry_run,
                "reason": "below min_memories_for_consolidation",
            }, indent=2), err=True)
        sys.exit(1)

    if fmt == "text":
        click.echo(
            f"Consolidated {result.candidates_found} memories "
            f"→ {result.consolidated_memory.id}"
        )
        return

    payload: dict[str, Any] = {
        "candidates_found": result.candidates_found,
        "consolidated_memory": _memory_to_dict(result.consolidated_memory),
        "deleted_ids": list(result.deleted_ids),
        "dry_run": dry_run,
    }
    click.echo(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
