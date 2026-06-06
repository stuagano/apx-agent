"""apx-agent — command-line interface for the apx-agent framework.

Subcommands:

  apx-agent scaffold <name>           Generate a new agent project
  apx-agent run                       Run the agent locally (uvicorn against create_app)
  apx-agent eval <evalset>            Run Mosaic AI Agent Evaluation
  apx-agent deploy                    Log to MLflow + deploy via databricks.agents.deploy
  apx-agent publish-tools             Publish @tool(uc=...) decorated tools to UC
  apx-agent publish                   Register the deployed endpoint as a Supervisor sub-agent
  apx-agent mcp-config                Emit the Managed MCP client config snippet
  apx-agent memory <cmd>              recall / remember / forget / list — MemoryStore CRUD
  apx-agent examples <cmd>            find / save / remove / list — ExampleStore CRUD
  apx-agent version                   Print the package version

Every command that operates on an agent accepts ``--module MODULE:VAR``
to point at the agent definition (defaults to ``agent:agent``). The module
must be importable from the current working directory.

The CLI is a thin orchestration layer over the primitives — every command
maps to a single ``apx_agent`` call. The CLI's value isn't logic; it's
ergonomics. Implementation should stay narrow.
"""

from __future__ import annotations

import difflib
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

from . import _doctor as _doctor_mod
from ._schema import introspect_schema_columns

logger = logging.getLogger(__name__)


def _fix_msg(title: str, detail: str, fix: str | None) -> str:
    """Consistent error body for hardened CLI failures."""
    parts = [title, detail]
    if fix:
        parts.append(f"\nFix:\n    {fix}")
    parts.append("\nRun `apx-agent doctor` for a full check.")
    return "\n".join(parts)


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


def _load_finalized_agent(module_spec: str) -> Any:
    """Resolve + finalize an agent for CLI commands.

    Loads [tool.apx.agent] config first, then resolve_agent so template-only
    projects (no agent.py) work via apx-agent info / lint / eval / run. Falls back
    to module-import for code-defined agents.
    """
    from ._wiring import finalize_agent, resolve_agent, _ws_for_template
    from ._inspection import _load_agent_config

    config = _load_agent_config(pyproject_path=None)
    agent = resolve_agent(module_spec, config, ws=_ws_for_template(config))
    finalize_agent(agent, config, pyproject_path=None)
    return agent


# ASGI app module spec served by `apx-agent run`, per scaffold layout.
_RUN_MODULE_BY_TARGET = {
    "model-serving": "app:app",
    "apps": "agent_server.start_server:app",
}


def _detect_target(cwd: Path | None = None) -> str:
    """Infer the project's target (model-serving vs apps) from its layout.

    The apps scaffold writes ``agent_server/start_server.py``; the flat
    model-serving scaffold writes a top-level ``app.py``. Lets ``apx-agent run`` and
    ``apx-agent deploy`` work without an explicit ``--module``/``--target`` regardless
    of which scaffold the user chose. Defaults to model-serving when neither
    marker is present.
    """
    cwd = cwd or Path.cwd()
    if (cwd / "agent_server" / "start_server.py").exists():
        return "apps"
    return "model-serving"


def _sanitize_uv_lock(lock_path: Path) -> bool:
    """Re-point a uv.lock's internal Databricks PyPI proxy at public PyPI.

    The dev's UV_INDEX_URL leaks ``pypi-proxy.dev.databricks.com`` into the
    lock's per-package ``source.registry``. Deployed apps / serving endpoints
    (and external users) can't reach that index, so any lock we ship must
    resolve from public PyPI. Download URLs are already public
    (files.pythonhosted.org); only the recorded index changes. Returns True if
    the file changed.
    """
    if not lock_path.exists():
        return False
    text = lock_path.read_text()
    proxy = "https://pypi-proxy.dev.databricks.com/simple"
    if proxy not in text:
        return False
    lock_path.write_text(text.replace(proxy, "https://pypi.org/simple"))
    return True


def _databrickscfg_profiles() -> list[str]:
    """Section names from ~/.databrickscfg (best-effort; includes DEFAULT)."""
    path = Path.home() / ".databrickscfg"
    if not path.exists():
        return []
    names = []
    for line in path.read_text(errors="ignore").splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            names.append(s[1:-1].strip())
    return names


def _preflight_databricks_auth() -> None:
    """Fail `apx-agent run`/`deploy` with dev-time guidance when auth is unresolved.

    Delegates to the doctor auth checks so inline errors and `apx-agent doctor` share
    one source of truth. Runs both the offline credential-resolution check and a
    live workspace round-trip so expired tokens are caught here rather than
    surfacing as confusing data errors mid-run.
    """
    from . import _doctor as _d

    result = _d.check_databricks_auth()
    if result.status is _d.Status.FAIL:
        raise click.ClickException(
            _fix_msg(
                "Could not resolve Databricks authentication. This agent "
                "connects to a workspace at startup.\n"
                "Run `apx-agent doctor` for a full environment check.",
                result.detail,
                result.fix,
            )
        )
    # Also verify the token is actually accepted by the workspace.
    live = _d.check_databricks_workspace(auth_ok=True)
    if live.status is _d.Status.FAIL:
        raise click.ClickException(
            _fix_msg(
                "Databricks credentials resolved but workspace rejected the token. "
                "Re-run `databricks auth login` to refresh.\n"
                "Run `apx-agent doctor` for details.",
                live.detail,
                live.fix,
            )
        )


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


class _ApxGroup(click.Group):
    """click.Group that suggests the closest command on a typo."""

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            cmd_name = args[0] if args else ""
            matches = difflib.get_close_matches(
                cmd_name, self.list_commands(ctx), n=1
            )
            hint = f" Did you mean `{matches[0]}`?" if matches else ""
            raise click.UsageError(f"No such command '{cmd_name}'.{hint}") from None


@click.group(cls=_ApxGroup)
@click.version_option(_resolve_version(), package_name="apx-agent", prog_name="apx")
def main() -> None:
    """apx-agent — declarative agents on Databricks. See `apx-agent --help` for commands."""


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
# doctor
# ---------------------------------------------------------------------------

_GLYPH = {
    _doctor_mod.Status.OK: "✓",
    _doctor_mod.Status.WARN: "⚠",
    _doctor_mod.Status.FAIL: "✗",
    _doctor_mod.Status.SKIP: "-",
}


@main.command()
@click.option("--offline", is_flag=True, help="Skip the live workspace check.")
@click.option("--json", "as_json", is_flag=True, help="Emit checks as JSON.")
def doctor(offline: bool, as_json: bool) -> None:
    """Diagnose the apx-agent environment: tools, auth, and project layout.

    Runs a live workspace round-trip by default; pass --offline to skip it.
    Exits non-zero if any check fails.
    """
    groups = _doctor_mod.run_checks(Path.cwd(), online=not offline)
    fails = sum(
        1 for _g, cs in groups for c in cs if c.status is _doctor_mod.Status.FAIL
    )
    warns = sum(
        1 for _g, cs in groups for c in cs if c.status is _doctor_mod.Status.WARN
    )

    if as_json:
        payload = {
            group: [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "detail": c.detail,
                    "fix": c.fix,
                }
                for c in checks
            ]
            for group, checks in groups
        }
        click.echo(json.dumps(payload, indent=2))
        sys.exit(1 if fails else 0)

    for group, checks in groups:
        click.echo(group)
        for c in checks:
            click.echo(f"  {_GLYPH[c.status]} {c.name}: {c.detail}")
            if c.fix and c.status in (_doctor_mod.Status.FAIL, _doctor_mod.Status.WARN):
                click.echo(f"      Fix: {c.fix}")
    click.echo("")
    if fails:
        click.echo(
            f"{fails} failed, {warns} warning(s). "
            "Fix the ✗ items, then re-run `apx-agent doctor`."
        )
        sys.exit(1)
    click.echo(f"All clear ({warns} warning(s)).")


# ---------------------------------------------------------------------------
# scaffold
# ---------------------------------------------------------------------------


_SCAFFOLD_PYPROJECT = '''\
[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.11"
# langgraph/langchain/databricks-langchain are required by the agent compile
# path — they're included as required deps in apx-agent's core package.
dependencies = ["apx-agent"]

[tool.uv.sources]
# Editable local install — keeps `uv sync` working from this directory.
# At deploy time apx-agent deploy builds a wheel and rewrites pyproject.toml to
# reference it (since the App container can't resolve ".."). Drop this block
# and change dependencies to `"apx-agent==<version>"` once published to PyPI.
apx-agent = {{ path = "..", editable = true }}

[tool.apx.agent]
name = "{name}"
description = "An apx-agent."
model = "databricks-claude-sonnet-4-6"
instructions = "You are a helpful assistant."

# Optional: declare resource tools as data (no code needed). `type` selects a
# platform factory; remaining keys are its arguments. See docs/configuration.md
# for the full type list and the APX_TOOLS_* trust controls.
# [[tool.apx.tools]]
# type = "genie"
# space_id = "$GENIE_SPACE_ID"
# name = "ask_data"
# description = "Answer questions from a Genie space."
'''


_SCAFFOLD_AGENT = '''\
"""Agent definition for {name}."""

from apx_agent import DataAgent

{example_tool}
# A governed data agent over ``{catalog}.{schema}`` (schema baked at scaffold
# time — no runtime discovery needed). To switch catalogs or refresh the
# baked schema: ``apx-agent scaffold <name> --catalog <cat> --schema <sch>``.
agent = DataAgent("{catalog}", "{schema}"{extra_tools})
'''


_SCAFFOLD_APP = '''\
"""FastAPI app entry point — for `apx-agent run` / Databricks Apps hosting."""

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
.apx-builder.json
"""


_SCAFFOLD_README = '''\
# {name}

An apx-agent.

## Local dev

```bash
uv sync
apx-agent run             # uvicorn against app.py:app
```

## Deploy to Model Serving

```bash
apx-agent publish-tools                                          # any @tool(uc=...) tools
apx-agent deploy --name main.agents.{name}                       # logs + deploys
apx-agent publish --endpoint {name} --supervisor SUPERVISOR_ID   # optional
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
    <APX_AGENT_DEP>
    # mlflow.genai.agent_server (imported by agent_server/start_server.py)
    # only exists in mlflow >=3.12; an older floor lets `uv sync` succeed but
    # the deployed App 502s with ModuleNotFoundError: mlflow.genai.
    "mlflow[databricks]>=3.12",
    # Add your agent's deps here
]

[project.scripts]
quickstart = "scripts.quickstart:main"

<APX_AGENT_SOURCE>
[tool.apx.agent]
name = "<APP_NAME>"
description = "An apx-agent on Databricks Apps."
model = "databricks-claude-sonnet-4-6"
instructions = "You are a helpful assistant."
# ADK-style layout: the user's agent definition lives at top-level
# ``agent.py``. ``agent_server/start_server.py`` is framework boilerplate
# that imports + wraps it for the Databricks Apps runtime.
module = "agent:agent"

# Starter prompts shown on the agent's landing page (clickable; fill the chat
# box so you can edit before sending). Tailor these to your agent's data/tools.
examples = [
    "Show me a few sample rows from the data you can access",
    "What questions can you answer?",
]

# Optional: declare resource tools as data (no code needed). `type` selects a
# platform factory; remaining keys are its arguments. See docs/configuration.md
# for the full type list and the APX_TOOLS_* trust controls.
# [[tool.apx.tools]]
# type = "genie"
# space_id = "$GENIE_SPACE_ID"
# name = "ask_data"
# description = "Answer questions from a Genie space."
'''


_SCAFFOLD_APPS_AGENT = '''\
"""<APP_NAME> — apx-agent root agent (ADK-style top-level definition).

Generated by ``apx-agent scaffold <APP_NAME> --target apps``.

Edit this file to define your agent. ``agent`` is the canonical top-level
symbol that ``agent_server/start_server.py`` imports + wraps for the
Databricks Apps runtime. The same symbol is consumed by
``apx-agent deploy --target model-serving`` if you also publish to a serving
endpoint.

The Apps runtime boilerplate — ``compile_to_responses_agent``,
``@invoke()`` / ``@stream()`` registration, MCP mounting — lives in
``agent_server/start_server.py`` so this file stays focused on the
agent's behavior.
"""
from __future__ import annotations

from apx_agent import DataAgent

<EXAMPLE_TOOL>
# A governed data agent over ``<CATALOG>.<SCHEMA>`` (schema baked at scaffold
# time — no runtime discovery needed). It answers questions grounded in that
# schema and queries via a SQL tool that runs as the calling user.
#
# Make it your own:
#   * point it at your own data:  DataAgent("main", "sales", name="<APP_NAME>")
#   * re-scaffold to refresh the baked schema:  apx-agent scaffold <name> --catalog <c>
#   * add ``genie_space=...`` / ``vector_index=...`` for Genie or Vector Search
#   * or drop back to a plain ``Agent(tools=[...])`` with your own ``@tool``s
agent = DataAgent("<CATALOG>", "<SCHEMA>"<EXTRA_TOOLS>, name="<APP_NAME>")
'''


_SCAFFOLD_APPS_AGENT_BASE = '''\
"""<APP_NAME> — apx-agent root agent.

Generated by ``apx-agent scaffold <APP_NAME> --template base``.

Edit this file to define your agent. ``agent`` is the symbol
``agent_server/start_server.py`` imports and wires into the
Databricks Apps runtime.
"""
from __future__ import annotations

from apx_agent import LlmAgent


# Define your tools here. A tool is any typed Python function with a docstring.
#
# Example:
#   def search_docs(query: str) -> str:
#       """Search internal documentation for a given query."""
#       return "..."
#
# Then add it: LlmAgent(tools=[search_docs], ...)

agent = LlmAgent(
    tools=[],
    instructions="You are a helpful assistant.",
    name="<APP_NAME>",
)
'''


_SCAFFOLD_APPS_AGENT_COWORKER = '''\
from __future__ import annotations

from apx_agent import CoworkerAgent

<EXAMPLE_TOOL>
# Add memory="persistent" to remember facts and context across sessions.
agent = CoworkerAgent("<CATALOG>", "<SCHEMA>"<EXTRA_TOOLS><PERSONA_ARG><JOIN_KEY_ARG><OBJECTIVE_ARG>, name="<APP_NAME>")
'''


_SCAFFOLD_APPS_START_SERVER = '''\
"""FastAPI entry point for Databricks Apps — AUTO-GENERATED by apx-agent scaffold.

Don't edit this file. Edit ``../agent.py`` instead — that's where your
agent lives. This file wires it into the Databricks Apps runtime:

  * Imports your ``agent`` from the top-level ``agent.py``.
  * Compiles it to the MLflow ResponsesAgent contract.
  * Registers ``@invoke`` / ``@stream`` handlers.
  * Mounts apx-agent's ``/mcp`` + ``/.well-known/agent.json`` + ``/health``
    so Genie / Genie Code can consume the same agent.
  * Mounts ``/readyz`` — a capability self-test that proves the agent answers
    and traces (used by ``apx-agent deploy`` as a readiness gate).

Run via ``uvicorn agent_server.start_server:app --host 0.0.0.0 --port $DATABRICKS_APP_PORT``
(driven by ``databricks.yml`` on deploy).
"""

from __future__ import annotations

import os

from mlflow.genai.agent_server import AgentServer, invoke, stream

from apx_agent import compile_to_responses_agent, mount_mcp_endpoints, mount_readyz
from apx_agent._defaults import _make_workspace_client
from apx_agent._inspection import _load_agent_config
from apx_agent._memory_wiring import resolve_session_store
from apx_agent._mlflow_tracing import autolog_if_env

# Enable MLflow LangChain/LangGraph auto-tracing when APX_AGENT_MLFLOW_AUTOLOG
# is set (databricks.yml sets it on deploy). Must run before the agent's
# LangChain components are built/invoked.
autolog_if_env()

# Import the user's agent from the top-level module.
from agent import agent

MODEL = os.environ.get("APX_MODEL", "databricks-claude-sonnet-4-6")

_ws = _make_workspace_client()
# Load pyproject.toml config so [tool.apx.agent.session] is honoured even when
# the agent constructor uses memory="off" (the default). Constructor-carried
# session_config still wins via resolve_session_store's precedence logic.
_agent_config = _load_agent_config()
_session_store = resolve_session_store(_agent_config, _ws, agent=agent)
_invoke_fn, _stream_fn = compile_to_responses_agent(agent, model=MODEL, session_store=_session_store)


@invoke()
def non_streaming(request):
    """Non-streaming request handler — POST /invocations."""
    return _invoke_fn(request)


@stream()
def streaming(request):
    """Streaming request handler — POST /invocations with stream=true."""
    yield from _stream_fn(request)


server = AgentServer(agent_type="ResponsesAgent")
app = server.app

mount_mcp_endpoints(app, agent)
mount_readyz(app, agent)

app.state.workspace_client = _ws
app.state.session_store = _session_store


if __name__ == "__main__":
    server.run("agent_server.start_server:app")
'''


_SCAFFOLD_APPS_QUICKSTART = '''\
"""quickstart — one-shot setup for the <APP_NAME> Apps deploy.

Creates the MLflow experiment for tracing and writes its ID to .env.
Provisions memory backends (Delta tables or Lakebase instance) if configured.
Safe to re-run; idempotent.
"""
from apx_agent.bootstrap import init_apps_experiment, provision_memory_backends


def main() -> None:
    path, exp_id = init_apps_experiment()
    print(f"MLflow experiment: {path} (id={exp_id})")

    for line in provision_memory_backends():
        print(line)


if __name__ == "__main__":
    main()
'''


_SCAFFOLD_MEMORY_BLOCK = '''
# Uncomment to enable persistent memory (Delta tables auto-created by `uv run quickstart`).
# Re-run quickstart after changing these table names.
# [tool.apx.agent.memory]
# type = "delta"
# table_name = "<CATALOG>.<SCHEMA>.apx_<APP_NAME_SLUG>_memory"
#
# [tool.apx.agent.session]
# type = "delta"
# table_name = "<CATALOG>.<SCHEMA>.apx_<APP_NAME_SLUG>_sessions"
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
# ``apx-agent deploy --target apps`` runs this script BEFORE ``bundle validate`` so
# the DAB validator sees a populated source dir. Manually copying the
# apx-agent wheel into the project keeps the App container's ``uv sync``
# off the public index — apx-agent deploy handles the wheel build for you when
# ``[tool.uv.sources].apx-agent`` references a local path.
artifacts:
  default:
    build: |
      mkdir -p .build
      # User-authored: top-level agent.py (+ optional tools.py, sub_agents/).
      cp agent.py .build/ 2>/dev/null || true
      cp tools.py .build/ 2>/dev/null || true
      cp -r sub_agents .build/ 2>/dev/null || true
      # Framework boilerplate + project files (don't edit agent_server/).
      cp -r agent_server scripts README.md .build/ 2>/dev/null || true
      cp -r .apx-agent .build/ 2>/dev/null || true
      cp apx_agent-*.whl .build/ 2>/dev/null || true
      # pyproject.toml and uv.lock are written by apx-agent deploy (wheel-pinned).
      # Do NOT copy them here — bundle deploy re-runs this script and would
      # overwrite the rewritten versions.

resources:
  experiments:
    <APP_NAME>_experiment:
      name: /Users/${var.workspace_user}/${bundle.name}-${bundle.target}

  apps:
    <APP_NAME>:
      name: <APP_NAME>
      description: <APP_NAME> apx-agent
      source_code_path: ./.build
      # OAuth scopes the forwarded user (OBO) token carries, so governed tools
      # run AS the calling user. The iam.* defaults are always granted and must
      # NOT be listed here. `sql` is what DataAgent / sql_tool / uc_function_tool
      # need; add `dashboards.genie` for genie_tool and `vectorsearch.vector-search-endpoints`
      # for vector_search_tool. (Changing scopes requires users to re-authorize.)
      user_api_scopes:
        - sql
        - serving.serving-endpoints
      resources:
        - name: experiment
          experiment:
            experiment_id: ${resources.experiments.<APP_NAME>_experiment.id}
            permission: CAN_MANAGE
        # apx-agent deploy --target apps will auto-add resources from the agent's
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
uv run apx-agent run --reload
# → FastAPI on http://localhost:8000 with the /_apx/* dev UI (chat, edit,
#   topology, traces, eval, setup wizard). Edit agent.py in your IDE;
#   --reload picks up changes. See docs/getting-started.md for the walkthrough.
curl -X POST http://localhost:8000/invocations -d '{"input":[{"role":"user","content":"hi"}]}'
```

## Deploy
```bash
apx-agent deploy --target apps  # validates, deploys, runs the bundle
```

## Edit
Define your agent + tools in `agent.py` (top-level). The
`agent_server/start_server.py` file is framework boilerplate that
imports your agent and wires it into the Databricks Apps runtime — you
shouldn't need to edit it.

> Tip: use underscore/snake_case for `<APP_NAME>` — Databricks bundle
> resource references like `${resources.experiments.<APP_NAME>_experiment.id}`
> are easier to read with snake_case names.
'''


def _discover_default_data(
    profile: str | None = None,
) -> "tuple[str, str, str | None] | None":
    """Best-effort: pick a (catalog, schema, sample_table) the scaffold's default
    DataAgent can actually read, so a fresh ``apx-agent run`` answers a real question
    immediately — and a baked example tool queries a real table.

    Prefers ``samples.nyctaxi`` (Databricks' built-in demo data) when present;
    otherwise the first accessible catalog/schema that has tables. Returns the
    triple, or None when auth can't be resolved / nothing readable is found —
    the caller falls back to the samples default. Scans are capped for speed.
    """
    try:
        from databricks.sdk import WorkspaceClient
        ws = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
    except Exception:
        return None

    def _first_table(cat: str, sch: str) -> str | None:
        try:
            for t in ws.tables.list(catalog_name=cat, schema_name=sch):
                if t.name:
                    return t.name
        except Exception:
            return None
        return None

    nyc = _first_table("samples", "nyctaxi")
    if nyc:
        return ("samples", "nyctaxi", nyc)

    skip_cat = {"system", "__databricks_internal"}
    skip_sch = {"information_schema"}
    try:
        cats = list(ws.catalogs.list())
    except Exception:
        return None
    probed = 0
    for cat in cats[:25]:
        cname = cat.name or ""
        if not cname or cname in skip_cat:
            continue
        try:
            schemas = list(ws.schemas.list(catalog_name=cname))
        except Exception:
            continue
        for sch in schemas[:25]:
            sname = sch.name or ""
            if not sname or sname in skip_sch:
                continue
            probed += 1
            if probed > 40:  # hard cap — keep scaffold snappy on big metastores
                return None
            tbl = _first_table(cname, sname)
            if tbl:
                return (cname, sname, tbl)
    return None


def _probe_first_table(catalog: str, schema: str, profile: str | None = None) -> str | None:
    """First readable table in ``catalog.schema`` (best-effort) for the example
    tool. Returns None when auth can't be resolved or the schema is empty."""
    try:
        from databricks.sdk import WorkspaceClient
        ws = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
        for t in ws.tables.list(catalog_name=catalog, schema_name=schema):
            if t.name:
                return t.name
    except Exception:
        return None
    return None


def _make_ws_for_scaffold(profile: str | None):
    """Build a WorkspaceClient for scaffold-time introspection, or None."""
    try:
        from databricks.sdk import WorkspaceClient
        return WorkspaceClient(profile=profile) if profile else WorkspaceClient()
    except Exception:
        return None


def _schema_manifest_for_scaffold(
    catalog: str, schema: str, profile: str | None = None
) -> "dict | None":
    """Introspect ``catalog.schema`` via the Tables API and return a manifest
    dict ``{catalog, schema, tables}``, or ``None`` when nothing is readable.

    Calls the module-level ``introspect_schema_columns`` (so tests can stub it).
    """
    ws = _make_ws_for_scaffold(profile)
    if ws is None:
        return None
    tables = introspect_schema_columns(ws, catalog, schema)
    if not tables:
        return None
    return {"catalog": catalog, "schema": schema, "tables": tables}


def _example_tool_block(catalog: str, schema: str, table: str | None) -> "tuple[str, str]":
    """Bake a 'talk to your data' example tool against a real table.

    Returns ``(prelude, extra_tools_arg)`` — the imports + tool function source,
    and the ``, extra_tools=[fn]`` snippet to splice into the DataAgent call.
    Empty strings when there's no known table (caller leaves the bare agent).
    """
    if not table:
        return ("", "")
    import re as _re
    fn = "sample_" + (_re.sub(r"\W", "_", table).strip("_") or "rows")
    prelude = (
        "from apx_agent import Dependencies\n"
        "from apx_agent.sql_tools import run_sql\n"
        "\n\n"
        f"def {fn}(ws: Dependencies.Workspace) -> dict:\n"
        f'    """Preview a few rows of `{catalog}.{schema}.{table}` — your first tool.\n'
        "\n"
        "    Runs as the calling user, so Unity Catalog enforces *their* grants\n"
        "    (auth passthrough — no token handling here). Add params + richer SQL\n"
        "    to make it your own, or generate more tools on the Edit page.\n"
        '    """\n'
        f'    return run_sql(ws, "SELECT * FROM `{catalog}`.`{schema}`.`{table}` LIMIT 5")\n'
        "\n\n"
    )
    return (prelude, f", extra_tools=[{fn}]")


# ---------------------------------------------------------------------------
# Workspace listing helpers — used by _interactive_resolve and apx-agent list
# ---------------------------------------------------------------------------

_SKIP_CATALOGS: frozenset[str] = frozenset({"system", "__databricks_internal"})
_SKIP_SCHEMAS: frozenset[str] = frozenset({"information_schema"})


def _ws_list_catalogs(ws: "Any", limit: int = 50) -> list[str]:
    try:
        return [c.name for c in ws.catalogs.list() if c.name and c.name not in _SKIP_CATALOGS][:limit]
    except Exception:
        return []


def _ws_list_schemas(ws: "Any", catalog: str, limit: int = 50) -> list[str]:
    try:
        return [
            s.name for s in ws.schemas.list(catalog_name=catalog)
            if s.name and s.name not in _SKIP_SCHEMAS
        ][:limit]
    except Exception:
        return []


def _ws_list_tables(ws: "Any", catalog: str, schema: str, limit: int = 100) -> list[Any]:
    try:
        return list(ws.tables.list(catalog_name=catalog, schema_name=schema))[:limit]
    except Exception:
        return []


def _ws_list_functions(ws: "Any", catalog: str, schema: str, limit: int = 100) -> list[Any]:
    try:
        return list(ws.functions.list(catalog_name=catalog, schema_name=schema))[:limit]
    except Exception:
        return []


def _scaffold_wizard(
    ws: "Any | None",
    target: "str | None",
    template: "str | None",
    catalog: "str | None",
    schema: "str | None",
) -> "tuple[str, str, str | None, str | None, str | None, str | None, str | None]":
    """Directive first-time setup wizard.

    Asks one question at a time, only for values not already pinned by a
    flag. Returns ``(target, template, catalog, schema, persona, objective, join_key)``.
    """
    # --- deployment target ---
    if target is None:
        click.echo("\nWhere will this agent run?")
        click.echo("  1. Databricks Apps   ← recommended (dev UI, one-command deploy)")
        click.echo("  2. Model Serving     (ChatAgent endpoint only)")
        idx = click.prompt("Target", type=click.IntRange(1, 2), default=1)
        target = "apps" if idx == 1 else "model-serving"

    # --- agent template ---
    if template is None:
        if target == "apps":
            click.echo("\nAgent type:")
            click.echo("  1. Base       — plain agent, you bring the tools")
            click.echo("  2. Data       — grounded in your UC schema, SQL tools included")
            click.echo("  3. Coworker   — joins two source systems; persona + join key + objective")
            idx = click.prompt("Template", type=click.IntRange(1, 3), default=2)
            template = {1: "base", 2: "data", 3: "coworker"}[idx]
        else:
            template = "data"

    # --- catalog / schema / persona / objective / join_key (not needed for base) ---
    if template in ("data", "coworker"):
        catalog, schema, persona, objective, join_key = _interactive_resolve(
            ws, catalog, schema, None, None, None, template
        )
    else:
        persona = objective = join_key = None

    return target, template, catalog, schema, persona, objective, join_key


def _interactive_resolve(
    ws: "Any | None",
    catalog: "str | None",
    schema: "str | None",
    persona: "str | None",
    objective: "str | None",
    join_key: "str | None",
    template: str,
) -> "tuple[str | None, str | None, str | None, str | None, str | None]":
    """Prompt for any missing catalog/schema/persona/objective/join_key.

    Already-provided values pass through unchanged. Falls back to free-text
    prompts when ws is None or the listing is empty. Returns
    ``(catalog, schema, persona, objective, join_key)``.
    """
    if catalog is None:
        cat_list = _ws_list_catalogs(ws, limit=20) if ws is not None else []
        if cat_list:
            click.echo("Available catalogs:")
            for i, c in enumerate(cat_list, 1):
                click.echo(f"  {i:2}. {c}")
            idx = click.prompt(
                "Choose a catalog",
                type=click.IntRange(1, len(cat_list)),
            )
            catalog = cat_list[idx - 1]
        else:
            catalog = click.prompt("Catalog (e.g. main, samples)")

    if schema is None:
        sch_list = _ws_list_schemas(ws, catalog, limit=20) if (ws is not None and catalog) else []
        if sch_list:
            click.echo(f"Available schemas in {catalog}:")
            for i, s in enumerate(sch_list, 1):
                click.echo(f"  {i:2}. {s}")
            idx = click.prompt(
                "Choose a schema",
                type=click.IntRange(1, len(sch_list)),
            )
            schema = sch_list[idx - 1]
        else:
            schema = click.prompt(f"Schema in {catalog}")

    if template == "coworker":
        if persona is None:
            raw = click.prompt(
                "Persona — who is this agent?\n"
                "  e.g. 'a payroll operations analyst', 'a fraud detection analyst'\n"
                "  Leave blank to skip",
                default="",
                show_default=False,
            )
            persona = raw.strip() or None

        if join_key is None:
            raw = click.prompt(
                "Join key — the business entity linking the two source systems\n"
                "  e.g. 'employee ID', 'opportunity ID', 'asset serial number'\n"
                "  Leave blank to skip",
                default="",
                show_default=False,
            )
            join_key = raw.strip() or None

        if objective is None:
            raw = click.prompt(
                "Objective — what is this agent designed to do?\n"
                "  e.g. 'surface mismatches between hours worked and paychecks issued'\n"
                "       'detect fraudulent transactions and flag anomalies'\n"
                "  Leave blank to skip",
                default="",
                show_default=False,
            )
            objective = raw.strip() or None

    return catalog, schema, persona, objective, join_key


def _scaffold_model_serving(
    target: Path, name: str, force: bool, catalog: str, schema: str,
    table: str | None = None,
) -> None:
    """Write the original Model Serving project layout into ``target``.

    Mirrors the pre-``--target`` shape: top-level ``agent.py`` + ``app.py``.
    """
    prelude, extra_tools = _example_tool_block(catalog, schema, table)

    import json as _json
    manifest = _schema_manifest_for_scaffold(catalog, schema)
    files = {
        "pyproject.toml": _SCAFFOLD_PYPROJECT.format(name=name),
        "agent.py": _SCAFFOLD_AGENT.format(
            name=name, catalog=catalog, schema=schema,
            example_tool=prelude, extra_tools=extra_tools,
        ),
        "app.py": _SCAFFOLD_APP,
        ".gitignore": _SCAFFOLD_GITIGNORE,
        "README.md": _SCAFFOLD_README.format(name=name),
    }
    if manifest is not None:
        files[".apx/schema.json"] = _json.dumps(manifest, indent=2)
    for rel_path, content in files.items():
        path = target / rel_path
        if path.exists() and not force:
            click.echo(f"  skip   {path} (exists; pass --force to overwrite)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        click.echo(f"  write  {path}")


def _is_inside_framework_repo(target: Path) -> bool:
    """True when ``target.parent`` is the apx-agent framework's ``python/``
    directory — the one layout where the editable ``path = ".."`` source in
    the scaffolded pyproject resolves to a real Python project.

    Anywhere else, the editable source breaks ``uv sync`` (the parent isn't
    a Python project), so the scaffold needs to emit a git+https install
    instead. Detection is: parent has a pyproject whose ``[project].name``
    is ``apx-agent``.
    """
    try:
        parent_pyproject = target.resolve().parent / "pyproject.toml"
        if not parent_pyproject.is_file():
            return False
        import tomllib
        data = tomllib.loads(parent_pyproject.read_text())
        return data.get("project", {}).get("name") == "apx-agent"
    except Exception:
        return False


def _find_nearby_framework_python(start: Path) -> Path | None:
    """Walk up from ``start`` looking for an apx-agent framework checkout.

    Returns the framework's ``python/`` subdir if found — the directory the
    user almost always wants to scaffold INSIDE, because the scaffolded
    pyproject's editable ``path = ".."`` source only resolves there. Returns
    ``None`` if no checkout is found anywhere on the path to the filesystem
    root, in which case the scaffold falls through to the git+https install.
    """
    import tomllib
    for candidate in [start.resolve(), *start.resolve().parents]:
        py_pyproject = candidate / "python" / "pyproject.toml"
        if not py_pyproject.is_file():
            continue
        try:
            data = tomllib.loads(py_pyproject.read_text())
        except Exception:
            continue
        if data.get("project", {}).get("name") == "apx-agent":
            return candidate / "python"
    return None


def _scaffold_install_ref() -> str:
    """Git ref to pin in scaffolded pyprojects when installing apx-agent from
    GitHub. Uses the running version (``v0.2.3``) when it's a clean release,
    else ``main`` for dev installs / unknown versions. The user can always
    rewrite the tag later — this just picks a sensible default."""
    import re
    from importlib.metadata import version
    try:
        v = version("apx-agent")
    except Exception:
        return "main"
    return f"v{v}" if re.match(r"^\d+\.\d+\.\d+$", v) else "main"


def _scaffold_apps(
    target: Path, name: str, force: bool, catalog: str, schema: str,
    table: str | None = None, template: str = "data",
    persona: str | None = None, objective: str | None = None,
    join_key: str | None = None,
) -> None:
    """Write a Databricks Apps-ready project layout into ``target``.

    Produces the ``agent_server/`` + ``scripts/`` + ``databricks.yml``
    bundle shape consumed by ``databricks bundle deploy``.
    """
    import json as _json

    if template == "base":
        prelude = extra_tools = ""
        manifest = None
    else:
        prelude, extra_tools = _example_tool_block(catalog, schema, table)
        manifest = _schema_manifest_for_scaffold(catalog, schema)

    # Pick the apx-agent install ref that will let ``uv sync`` succeed from
    # this scaffold directory. Inside the framework repo, use the editable
    # parent-dir source (fast dev loop). Outside, embed a pinned git+https
    # URL in the dep line so the user doesn't need a sibling checkout.
    if _is_inside_framework_repo(target):
        apx_dep = '"apx-agent",'
        apx_source = (
            "[tool.uv.sources]\n"
            "# Editable local install — keeps `uv sync` working from this directory.\n"
            "# At deploy time apx-agent deploy builds a wheel and rewrites .build/pyproject.toml\n"
            "# to reference it (since the App container can't resolve \"..\").\n"
            'apx-agent = { path = "..", editable = true }\n'
        )
    else:
        ref = _scaffold_install_ref()
        apx_dep = (
            f'"apx-agent @ '
            f'git+https://github.com/stuagano/apx-agent.git@{ref}#subdirectory=python",'
        )
        apx_source = (
            "# [tool.uv.sources] intentionally omitted — the git+https URL in\n"
            "# [project].dependencies above is the install source. Bump the @ref\n"
            "# in that URL to upgrade; switch to a PyPI pin once apx-agent ships there.\n"
        )

    persona_arg = f", persona={repr(persona)}" if persona else ""
    objective_arg = f", objective={repr(objective)}" if objective else ""
    join_key_arg = f", join_key={repr(join_key)}" if join_key else ""

    import re as _re
    name_slug = _re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "agent"

    def _sub(template: str) -> str:
        return (
            template.replace("<APP_NAME>", name)
            .replace("<APP_NAME_SLUG>", name_slug)
            .replace("<CATALOG>", catalog)
            .replace("<SCHEMA>", schema)
            .replace("<EXAMPLE_TOOL>", prelude)
            .replace("<EXTRA_TOOLS>", extra_tools)
            .replace("<PERSONA_ARG>", persona_arg)
            .replace("<OBJECTIVE_ARG>", objective_arg)
            .replace("<JOIN_KEY_ARG>", join_key_arg)
            .replace("<APX_AGENT_DEP>", apx_dep)
            .replace("<APX_AGENT_SOURCE>", apx_source)
        )

    # Inject commented-out memory block when a UC catalog/schema is known (coworker / data agents).
    pyproject = _sub(
        _SCAFFOLD_APPS_PYPROJECT + (_SCAFFOLD_MEMORY_BLOCK if (catalog and schema) else "")
    )

    files: dict[str, str] = {
        "pyproject.toml": pyproject,
        "databricks.yml": _sub(_SCAFFOLD_APPS_DATABRICKS_YML),
        ".env.example": _SCAFFOLD_APPS_ENV_EXAMPLE,
        ".gitignore": _SCAFFOLD_GITIGNORE,
        "README.md": _sub(_SCAFFOLD_APPS_README),
        # ADK-style layout: the user's agent definition lives at top-level
        # ``agent.py``. ``agent_server/`` is framework boilerplate only.
        "agent.py": (
            _SCAFFOLD_APPS_AGENT_BASE.replace("<APP_NAME>", name) if template == "base"
            else _sub(_SCAFFOLD_APPS_AGENT_COWORKER if template == "coworker" else _SCAFFOLD_APPS_AGENT)
        ),
        "agent_server/__init__.py": "",
        "agent_server/start_server.py": _SCAFFOLD_APPS_START_SERVER,
        "scripts/__init__.py": "",
        "scripts/quickstart.py": _sub(_SCAFFOLD_APPS_QUICKSTART),
    }
    if manifest is not None:
        files[".apx/schema.json"] = _json.dumps(manifest, indent=2)
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
    type=click.Choice(["apps", "model-serving"]),
    default=None,
    help=(
        "Runtime to generate scaffolding for. "
        "'apps' (default) generates a Databricks Apps bundle: agent_server/ + "
        "databricks.yml, with the built-in dev UI (chat, edit, tool builder), "
        "deployed via apx-agent deploy. 'model-serving' generates the flat "
        "agent.py + app.py layout for a Mosaic AI ChatAgent serving endpoint. "
        "Prompted interactively when omitted in a TTY."
    ),
)
@click.option("--force", is_flag=True, help="Overwrite existing files.")
@click.option(
    "--here", is_flag=True,
    help=(
        "Stay in the current directory even if an apx-agent framework "
        "checkout is detected nearby. By default the scaffold redirects "
        "into the framework's python/ subdir so the editable install "
        "resolves; use --here to override and emit a git+https install."
    ),
)
@click.option(
    "--catalog", default=None,
    help="Catalog for the default DataAgent. Skips workspace auto-detection.",
)
@click.option(
    "--schema", default=None,
    help="Schema for the default DataAgent. Skips workspace auto-detection.",
)
@click.option(
    "--profile", default=None,
    help="Databricks CLI profile used to probe the workspace for a default "
         "catalog/schema. Falls back to $DATABRICKS_CONFIG_PROFILE.",
)
@click.option(
    "--template", "scaffold_template",
    type=click.Choice(["base", "data", "coworker"]),
    default=None,
    help=(
        "Agent kind: 'base' (bare LlmAgent, you bring the tools), "
        "'data' (pre-grounded SQL agent), or 'coworker' "
        "(DataAgent + persona). Apps target only for coworker. "
        "Prompted interactively when omitted in a TTY."
    ),
)
@click.option(
    "--interactive/--no-interactive", "interactive", default=None,
    help="Run the setup wizard (target, template, catalog, schema, persona). "
         "Defaults to on when stdin is a TTY.",
)
def scaffold(
    name: str, directory: str, scaffold_target: str | None, force: bool, here: bool,
    catalog: str | None, schema: str | None, profile: str | None,
    scaffold_template: str | None, interactive: bool | None,
) -> None:
    """Generate a new agent project at <NAME>.

    With ``--target apps`` (the default) writes a Databricks Apps bundle:
    ``agent_server/`` package + ``databricks.yml`` + the dev UI, deployable
    with ``apx-agent deploy``. With ``--target model-serving`` writes a flat
    ``agent.py``/``app.py`` project for a Mosaic AI ChatAgent serving endpoint.

    The default agent is a ``DataAgent`` grounded in real data: unless you pass
    ``--catalog``/``--schema``, the workspace is probed (best-effort) for a
    catalog/schema you can read — preferring ``samples.nyctaxi`` — so a fresh
    ``apx-agent run`` can answer a real question immediately.
    """
    target = Path(directory) / name

    # Auto-redirect into the framework's python/ subdir when the user is
    # standing in (or under) an apx-agent checkout but not at the right level.
    # Without this, a scaffold at e.g. ~/apx-agent/MyAgent embeds an editable
    # `path = ".."` that resolves to ~/apx-agent — which isn't a Python project
    # — and `uv sync` fails with a confusing "not a Python project" error.
    if not here and directory == ".":
        framework_python = _find_nearby_framework_python(Path.cwd())
        if framework_python is not None and framework_python != Path.cwd().resolve():
            new_target = framework_python / name
            click.echo(
                f"apx-agent framework checkout detected at {framework_python.parent}.\n"
                f"Scaffolding into python/ for the editable install:\n"
                f"  {Path.cwd().resolve() / name}\n"
                f"  → {new_target}\n"
                f"(use --here to stay at the current location with a git+https install)\n",
                err=True,
            )
            target = new_target

    # ``name`` may be a path (e.g. "examples/my-agent"); the project/agent name
    # baked into the templates must be the bare directory name, or it produces
    # an invalid PEP 508 ``[project].name`` that breaks ``uv sync``.
    project_name = Path(name).name
    if target.exists() and not force:
        if any(target.iterdir()):
            raise click.ClickException(
                f"{target} already exists and is not empty. Pass --force to overwrite."
            )
    target.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Step 1: run the setup wizard or apply CLI defaults.
    # The wizard fires in a TTY (or with --interactive) and asks for anything
    # not already pinned by a flag. --no-interactive / CI stdin skips it.
    # -----------------------------------------------------------------------
    persona: str | None = None
    objective: str | None = None
    interactive_mode = interactive if interactive is not None else sys.stdin.isatty()

    join_key: str | None = None
    if interactive_mode:
        scaffold_target, scaffold_template, catalog, schema, persona, objective, join_key = _scaffold_wizard(
            ws=_make_ws_for_scaffold(profile),
            target=scaffold_target,
            template=scaffold_template,
            catalog=catalog,
            schema=schema,
        )
    else:
        scaffold_target = scaffold_target or "apps"
        scaffold_template = scaffold_template or "data"

    # -----------------------------------------------------------------------
    # Step 2: validate the combination before touching the filesystem.
    # -----------------------------------------------------------------------
    if scaffold_target == "model-serving" and scaffold_template == "coworker":
        raise click.ClickException(
            "--template coworker requires --target apps "
            "(model-serving coworker scaffold is a follow-up)."
        )

    # -----------------------------------------------------------------------
    # Step 3: resolve the data source — explicit > auto-detect > demo fallback.
    # Base template has no data source; skip entirely.
    # -----------------------------------------------------------------------
    table: str | None = None
    if scaffold_template == "base":
        catalog = catalog or ""
        schema = schema or ""
    elif catalog and schema:
        table = _probe_first_table(catalog, schema, profile)
        extra = f" (example tool over `{table}`)" if table else ""
        click.echo(f"# data source: {catalog}.{schema}{extra}")
    else:
        found = _discover_default_data(profile)
        if found:
            catalog, schema, table = found
            extra = f" (example tool over `{table}`)" if table else ""
            click.echo(
                f"# data source: {catalog}.{schema} — auto-detected from your "
                f"workspace{extra}"
            )
        else:
            catalog, schema = "samples", "nyctaxi"
            click.echo(
                "# data source: samples.nyctaxi (default — couldn't probe the "
                "workspace; pass --catalog/--schema to ground the agent in your data)"
            )

    if scaffold_target == "apps":
        _scaffold_apps(
            target, project_name, force, catalog, schema, table,
            template=scaffold_template, persona=persona, objective=objective,
            join_key=join_key,
        )
    else:
        _scaffold_model_serving(target, project_name, force, catalog, schema, table)

    click.echo()
    click.echo(f"Scaffolded {name} at {target} (target={scaffold_target}).")
    click.echo(f"Next: cd {name} && uv sync && apx-agent run    # serve locally")
    click.echo("Tip: run `apx-agent doctor` to check your environment before deploying.")
    if scaffold_target == "apps":
        click.echo("      apx-agent deploy                        # → Databricks Apps")
    else:
        click.echo(
            "      apx-agent deploy --model <endpoint> --name <catalog.schema.model>"
        )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _probe_import(module_spec: str) -> None:
    """Import the ASGI module in-process to surface agent.py errors clearly.

    `module_spec` is "module:variable"; we import the module half so a broken
    `agent.py` produces a clean file+line message instead of a uvicorn
    subprocess traceback. The CWD must already be on sys.path (the caller
    ensures this for the real run via app_dir).

    Under --reload uvicorn re-imports in a child process, so module-level code
    runs in both the probe and the child; this is fine for the framework's
    lifespan/AgentServer pattern which defers connections out of import time.
    """
    import importlib
    import traceback

    mod_name = _parse_module_spec(module_spec)[0]
    cwd = str(Path.cwd())
    added = cwd not in sys.path
    if added:
        sys.path.insert(0, cwd)
    try:
        importlib.import_module(mod_name)
    except Exception as e:  # noqa: BLE001 — surface any import-time failure
        tb = traceback.format_exc(limit=5).splitlines()
        file_frame = next(
            (line for line in reversed(tb) if line.strip().startswith('File "')),
            None,
        )
        detail = f"{type(e).__name__}: {e}"
        if file_frame:
            detail = f"{file_frame.strip()}\n    {detail}"
        raise click.ClickException(
            _fix_msg(
                f"Failed to import your agent module `{mod_name}`.",
                detail,
                "Fix the error in your agent code shown above, then re-run "
                "`apx-agent run`.",
            )
        ) from e
    finally:
        if added:
            sys.path.remove(cwd)


@main.command("refresh-schema")
@click.option("--profile", default=None,
              help="Databricks CLI profile to introspect with. "
                   "Falls back to $DATABRICKS_CONFIG_PROFILE.")
def refresh_schema(profile: str | None) -> None:
    """Re-introspect this project's catalog.schema and rewrite .apx/schema.json.

    Run inside a scaffolded project. Reads the existing manifest to learn which
    catalog/schema to refresh, re-introspects via the Tables API, and overwrites
    the manifest so the agent's grounding + landing card reflect the live schema.
    """
    import json as _json
    from ._schema import load_baked_schema, APX_DIR, SCHEMA_MANIFEST_NAME

    existing = load_baked_schema(Path.cwd())
    if not existing or not existing.get("catalog") or not existing.get("schema"):
        raise click.ClickException(
            "no .apx/schema.json found in this project — run `apx-agent scaffold` first "
            "(or create the manifest) so I know which catalog.schema to refresh."
        )
    catalog, schema = existing["catalog"], existing["schema"]
    manifest = _schema_manifest_for_scaffold(catalog, schema, profile=profile)
    if manifest is None:
        raise click.ClickException(
            f"could not read tables for {catalog}.{schema} — check your profile "
            f"and Unity Catalog grants."
        )
    out = Path.cwd() / APX_DIR / SCHEMA_MANIFEST_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_json.dumps(manifest, indent=2))
    n = len(manifest["tables"])
    click.echo(f"refreshed {out} — {n} table{'s' if n != 1 else ''} from {catalog}.{schema}")


@main.command()
@click.option(
    "--module",
    default=None,
    help='ASGI app module spec, "module:variable". Auto-detected from the '
         'project layout when omitted: "app:app" (model-serving) or '
         '"agent_server.start_server:app" (apps).',
)
@click.option("--port", default=8000, type=int, help="Port. Default: 8000.")
@click.option("--host", default="127.0.0.1", help="Host. Default: 127.0.0.1.")
@click.option("--reload", is_flag=True, help="Enable auto-reload for dev.")
def run(module: str | None, port: int, host: str, reload: bool) -> None:
    """Run the agent locally via uvicorn against the FastAPI app.

    Works for both scaffold layouts — the ASGI module is auto-detected from
    the project unless you pass --module.
    """
    try:
        import uvicorn
    except ImportError as e:
        raise click.ClickException(
            "uvicorn is required for `apx-agent run`. Install with: "
            "pip install 'uvicorn[standard]'"
        ) from e
    if module is None:
        detected = _detect_target()
        module = _RUN_MODULE_BY_TARGET[detected]
        click.echo(f"# Detected {detected} layout → serving {module}", err=True)
        if detected == "apps":
            model = os.environ.get("APX_MODEL", "databricks-claude-sonnet-4-6")
            click.echo(
                f"# apps runtime uses APX_MODEL={model} (export APX_MODEL to override)",
                err=True,
            )
    _preflight_databricks_auth()
    # Default ON for the dev loop: the Trace panel is useless without per-tool
    # and per-LLM spans, and the runtime's manual ``safe_span`` wraps only emit
    # the outer wrapper. ``setdefault`` so anyone who really wants the bare
    # boot can ``APX_AGENT_MLFLOW_AUTOLOG=0 apx-agent run``. Deploy paths are
    # unaffected — they never go through this code.
    #
    # Important: the ``apps`` scaffold's ASGI module bypasses ``_wiring.create_app``
    # (it uses ``mlflow.genai.agent_server.AgentServer`` directly), so the
    # autolog call that lives in that lifespan never runs in the dev loop.
    # We call it here, in-process, *before* uvicorn imports the user's
    # ``agent`` module — autolog has to be active before LangChain components
    # are instantiated for the instrumentation to attach.
    os.environ.setdefault("APX_AGENT_MLFLOW_AUTOLOG", "1")
    _experiment = _read_apx_agent_config().get("experiment")
    if _experiment:
        try:
            import mlflow as _mlflow
            _mlflow.set_experiment(_experiment)
            click.echo(f"# MLflow experiment: {_experiment}", err=True)
        except Exception as exc:  # pragma: no cover — defensive
            click.echo(f"# MLflow set_experiment skipped: {exc}", err=True)
    try:
        from ._mlflow_tracing import autolog_if_env
        autolog_if_env()
    except Exception as exc:  # pragma: no cover — defensive
        click.echo(f"# MLflow autolog setup skipped: {exc}", err=True)
    # app_dir puts the project dir on sys.path so the scaffold's app module
    # resolves. The `apx` console-script's sys.path does NOT include the CWD
    # (unlike `python`/`uvicorn` invoked directly), so without this uvicorn
    # reports `Could not import module "app"`. app_dir also propagates to the
    # --reload subprocess, which a bare sys.path.insert here would not.
    _probe_import(module)
    uvicorn.run(module, host=host, port=port, reload=reload, app_dir=str(Path.cwd()))


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------


@main.command("eval")
@click.argument("evalset", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--module",
    default=None,
    help='Agent module spec. Default: "agent:agent". Only used for in-process eval. '
         'Mutually exclusive with --endpoint-url.',
)
@click.option(
    "--model", default=None,
    help="Databricks serving endpoint for the LLM. Required for in-process eval. "
         "Mutually exclusive with --endpoint-url.",
)
@click.option(
    "--endpoint-url", default=None,
    help="Base URL of a deployed Databricks App (e.g. "
         "https://my-agent.<workspace>.databricksapps.com). When set, eval runs "
         "against the App's /invocations endpoint over HTTP. Mutually exclusive "
         "with --model and --module.",
)
@click.option(
    "--token", default=None,
    help="Databricks bearer token for --endpoint-url. Falls back to "
         "$DATABRICKS_TOKEN then `databricks auth token --profile $DATABRICKS_CONFIG_PROFILE`.",
)
@click.option(
    "--profile", default=None,
    help="Databricks CLI profile for the token-resolution fallback when "
         "--endpoint-url is set. Falls back to $DATABRICKS_CONFIG_PROFILE.",
)
@click.option(
    "--stream/--no-stream", default=True,
    help="Stream the /invocations endpoint (default). --no-stream waits for a "
         "single JSON body. Only used with --endpoint-url.",
)
@click.option(
    "--user-token", default=None,
    help="Optional OBO user token to evaluate under a specific user identity. "
         "Only used for in-process eval.",
)
@click.option(
    "--experiment", default=None,
    help="MLflow experiment name/path. Falls back to [tool.apx.agent].experiment "
         "in pyproject.toml; falls back to MLflow's default when neither is set.",
)
def eval_cmd(
    evalset: str,
    module: str | None,
    model: str | None,
    endpoint_url: str | None,
    token: str | None,
    profile: str | None,
    stream: bool,
    user_token: str | None,
    experiment: str | None,
) -> None:
    """Run Mosaic AI Agent Evaluation against EVALSET.

    Two modes:

      In-process (default): compile the agent and run eval directly.
        apx-agent eval evalset.jsonl --model databricks-claude-sonnet-4-6

      Endpoint: drive a deployed App's /invocations endpoint over HTTP.
        apx-agent eval evalset.jsonl --endpoint-url https://<app>.databricksapps.com
    """
    # Mutex: --endpoint-url is incompatible with the in-process eval flags.
    if endpoint_url and (model or module):
        offenders = [f for f, v in [("--model", model), ("--module", module)] if v]
        raise click.UsageError(
            f"--endpoint-url is mutually exclusive with {', '.join(offenders)}. "
            "Pass --endpoint-url to evaluate against a deployed App, or "
            "--model/--module to compile and evaluate in-process — not both."
        )

    # Auto-detect JSON / JSONL / YAML / CSV. mlflow.genai.evaluate accepts
    # most of these via the data kwarg, but only some via path — we read
    # JSON/JSONL ourselves for predictability.
    data: Any
    path = Path(evalset)
    suffix = path.suffix.lower()
    try:
        if suffix == ".jsonl":
            data = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        elif suffix == ".json":
            data = json.loads(path.read_text())
        else:
            # Forward path verbatim — mlflow handles CSV / Parquet / etc.
            data = evalset
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Failed to parse evalset {evalset}: {e}") from e

    effective_experiment = experiment or _read_apx_agent_config().get("experiment")

    if endpoint_url:
        from apx_agent import eval_against_endpoint

        try:
            result = eval_against_endpoint(
                endpoint_url,
                data,
                token=token,
                profile=profile,
                stream=stream,
                experiment=effective_experiment,
            )
        except RuntimeError as e:
            raise click.ClickException(str(e)) from e
        click.echo(f"Eval complete. Result: {result}")
        return

    # In-process eval path.
    if model is None:
        raise click.UsageError(
            "--model is required for in-process eval. "
            "Pass --endpoint-url to evaluate a deployed App instead."
        )
    effective_module = module or "agent:agent"

    from apx_agent import evaluate

    agent = _load_finalized_agent(effective_module)
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
              '"agent:agent" for both --target model-serving and '
              '--target apps (ADK-style top-level layout).')
@click.option(
    "--target", "target", default=None,
    type=click.Choice(["model-serving", "apps"]),
    help="Deployment target. Auto-detected from the project layout when "
         "omitted (apps if agent_server/start_server.py exists, else "
         "model-serving). 'model-serving' runs the canonical log_agent + "
         "databricks.agents.deploy flow. 'apps' runs the Databricks Asset "
         "Bundle deploy + run flow. The two targets accept different option "
         "sets — see --help for details.",
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
    "--readyz-gate/--no-readyz-gate", default=True,
    help="After the app reaches RUNNING, gate the deploy on its `/readyz` "
         "capability self-test: GET <app_url>/readyz and FAIL the deploy if "
         "the agent doesn't report ready (i.e. it doesn't actually answer + "
         "trace). A green deploy then means the agent works, not just that the "
         "container booted. ON by default. Only used by --target apps.",
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
         "the agent shows up in apx-agent list / topology / watchdog crawls. On by default.",
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
    target: str | None,
    model: str | None,
    registered_model_name: str | None,
    profile: str | None,
    bundle_target: str,
    no_run: bool,
    auto_update_yml: bool,
    auto_build_wheel: bool,
    auto_experiment: bool,
    readyz_gate: bool,
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
    if target is None:
        target = _detect_target()
        click.echo(
            f"# Detected {target} project layout (override with --target).",
            err=True,
        )

    if target == "apps":
        _deploy_apps(
            module=module or "agent:agent",
            profile=profile,
            bundle_target=bundle_target,
            no_run=no_run,
            auto_update_yml=auto_update_yml,
            auto_build_wheel=auto_build_wheel,
            auto_experiment=auto_experiment,
            vars=vars,
            json_output=json_output,
            readyz_gate=readyz_gate,
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

    # E3a: resolve_agent handles both template-only and module-defined agents.
    # Model-serving deploy still defers finalization to log_agent.
    from ._wiring import resolve_agent as _resolve_agent, _ws_for_template as _deploy_ws
    from ._inspection import _load_agent_config as _load_cfg
    _deploy_config = _load_cfg(pyproject_path=None)
    agent = _resolve_agent(effective_module, _deploy_config, ws=_deploy_ws(_deploy_config))
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

    # MLflow captures the project's uv.lock into the logged model. Re-point its
    # internal Databricks PyPI proxy at public PyPI so the deployed serving
    # endpoint can install deps (it can't reach the dev proxy).
    if _sanitize_uv_lock(Path.cwd() / "uv.lock"):
        click.echo("# sanitized uv.lock → public PyPI for the logged model", err=True)

    # 2. Log + register
    #
    # (Config knobs + persona overlay + declared tools are applied inside
    # log_agent via finalize_agent — no separate call needed here.)

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
        deployment = agents.deploy(registered_model_name, model_version=info.registered_model_version)
        endpoint_name = getattr(deployment, "endpoint_name", None) or registered_model_name.rsplit(".", 1)[-1]
        click.echo(
            f"Deployed {registered_model_name} version "
            f"{info.registered_model_version} as a serving endpoint."
        )
        click.echo(f"  Endpoint: {endpoint_name}")
        click.echo("  View in workspace: Serving → Endpoints")
    else:
        click.echo("Skipping deploy (--no-deploy).")

    # 4. Set UC tags so the agent shows up in apx-agent list / topology / watchdog
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
            "`apx-agent scaffold <name> --target apps` first."
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
            f"./apx_agent-{src_ver}-py3-none-any.whl, then re-run apx-agent "
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
            f"  cp dist/apx_agent-*.whl {cwd}/"
        )

    dist_dir = source_root / "dist"
    if expected_name is None:
        # Dynamic (hatch-vcs) version: the wheel name isn't knowable before the
        # build, so resolve it from what was actually produced (newest wheel).
        produced = sorted(
            dist_dir.glob("apx_agent-*.whl"), key=lambda p: p.stat().st_mtime
        )
        if not produced:
            raise click.ClickException(
                f"`uv build --wheel` succeeded in {source_root} but produced no "
                f"apx_agent-*.whl in {dist_dir}."
            )
        built = produced[-1]
        wheel_target = cwd / built.name
    else:
        built = dist_dir / expected_name
        if not built.exists():
            # Build succeeded but the produced wheel doesn't match the name
            # pyproject expects. List what's in dist/ so the user can spot it.
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


def _stage_build_manifest(
    build_dir: Path, wheel_path: Path | None,
) -> None:
    """Stage the dependency manifest (``pyproject.toml`` + ``uv.lock``) into ``.build/``.

    The bundle ``artifacts.build`` script deliberately does NOT copy
    ``pyproject.toml`` / ``uv.lock`` — ``apx-agent deploy`` owns staging the
    deploy-correct versions here. Without them the Databricks Apps container
    has no dependency manifest and falls back to the base image's packages
    (notably an older ``mlflow`` lacking ``mlflow.genai.agent_server``), so the
    app 502s on startup (issue #116).

    Two install shapes:

    - **wheel_path is None** (git+https / PyPI install — the scaffold default
      outside the framework repo): the deps already resolve remotely, so copy
      the source ``pyproject.toml`` + ``uv.lock`` into ``.build/`` verbatim.
      If the source has no ``uv.lock``, stage ``pyproject.toml`` alone and let
      the container re-resolve.

    - **wheel_path set** (editable/local install — inside the framework repo):
      the App container can't resolve ``..``, so rewrite the staged
      ``pyproject.toml`` to point at the bundled wheel and regenerate
      ``uv.lock`` against it.

    Either way the staged ``uv.lock`` is sanitized to public PyPI so the
    container's ``uv sync`` doesn't hit the internal proxy.
    """
    import shutil

    if wheel_path is None:
        # git+https / PyPI install: stage source manifest verbatim. The deps
        # resolve from the remote URL pinned in pyproject; no rewrite needed.
        source_pyproject = build_dir.parent / "pyproject.toml"
        if not source_pyproject.exists():
            return
        pyproject = build_dir / "pyproject.toml"
        shutil.copy2(source_pyproject, pyproject)
        click.echo("  staged .build/pyproject.toml (git+https install)", err=True)

        source_lock = build_dir.parent / "uv.lock"
        lockfile = build_dir / "uv.lock"
        if source_lock.exists():
            shutil.copy2(source_lock, lockfile)
            click.echo("  staged .build/uv.lock", err=True)
            if _sanitize_uv_lock(lockfile):
                click.echo("  sanitized .build/uv.lock → public PyPI", err=True)
        else:
            click.echo(
                "  no source uv.lock — container will re-resolve from pyproject",
                err=True,
            )
        return

    import subprocess

    try:
        import tomllib  # py>=3.11
    except ImportError:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]

    pyproject = build_dir / "pyproject.toml"
    if not pyproject.exists():
        # Artifacts script no longer copies pyproject.toml — read from source.
        source_pyproject = build_dir.parent / "pyproject.toml"
        if not source_pyproject.exists():
            return
        import shutil
        shutil.copy2(source_pyproject, pyproject)
    text = pyproject.read_text()
    try:
        doc = tomllib.loads(text)
    except Exception:
        return

    # Always write .build/pyproject.toml from the source so bundle deploy's
    # re-run of the artifacts script (which no longer copies pyproject.toml)
    # leaves the correct wheel-pinned version in place.
    sources = doc.get("tool", {}).get("uv", {}).get("sources", {})
    apx_source = sources.get("apx-agent") if isinstance(sources, dict) else None
    has_editable = (
        isinstance(apx_source, dict)
        and isinstance(apx_source.get("path"), str)
        and not apx_source["path"].endswith(".whl")
    )

    wheel_rel = f"./{wheel_path.name}"
    if has_editable:
        # Swap the editable line for the wheel path in-place, preserving
        # comments and ordering.
        new_block = f'apx-agent = {{ path = "{wheel_rel}" }}'
        lines = text.splitlines()
        swapped = False
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("apx-agent ") and "=" in stripped and "{" in stripped:
                lines[i] = new_block
                swapped = True
                break
        if swapped:
            text = "\n".join(lines) + "\n"
            click.echo(
                f"  rewrote .build/pyproject.toml: apx-agent -> {wheel_rel}", err=True,
            )

    pyproject.write_text(text)

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
    if _sanitize_uv_lock(lockfile):
        click.echo("  sanitized .build/uv.lock → public PyPI", err=True)


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


def _grant_experiment_to_sp(
    experiment_id: str,
    sp_client_id: str,
    *,
    profile: str | None,
) -> bool:
    """Grant the app's service principal ``CAN_MANAGE`` on the tracing experiment.

    ``apx-agent deploy`` creates the experiment under the deploying user, but the
    deployed app runs as its service principal — which can't access that
    experiment, so every span is dropped. Granting the SP ``CAN_MANAGE`` via
    the permissions API lets tracing land. Idempotent.

    Best-effort: any failure (non-zero exit, missing CLI, etc.) is logged to
    stderr and swallowed — never fail the deploy on the grant. Returns ``True``
    only when the grant succeeded.
    """
    import subprocess

    acl = {
        "access_control_list": [
            {
                "service_principal_name": sp_client_id,
                "permission_level": "CAN_MANAGE",
            }
        ]
    }
    cmd = [
        "databricks",
        "api",
        "patch",
        f"/api/2.0/permissions/experiments/{experiment_id}",
        "--json",
        json.dumps(acl),
    ]
    if profile:
        cmd += ["--profile", profile]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            click.echo(
                "# could not grant app SP access to tracing experiment "
                f"{experiment_id}: {_tail_lines(proc.stderr or proc.stdout, 3)}",
                err=True,
            )
            return False
        return True
    except Exception as exc:
        click.echo(
            "# could not grant app SP access to tracing experiment "
            f"{experiment_id}: {exc}",
            err=True,
        )
        return False


def _check_readyz(
    app_url: str,
    *,
    profile: str | None,
    attempts: int = 5,
    delay_s: float = 6.0,
) -> tuple[bool, dict[str, Any]]:
    """Call the deployed app's ``/readyz`` capability self-test.

    Mints a short-lived bearer token via ``databricks auth token`` (Databricks
    Apps require auth) and issues an authenticated ``GET <app_url>/readyz``.
    ``/readyz`` returns HTTP 200 ``{"status":"ready",...}`` when the agent
    answers + traces, and HTTP 503 ``{"status":"degraded",...}`` otherwise —
    both are valid JSON bodies to parse.

    Returns ``(status == "ready", checks_dict)``. The app may need a few
    seconds after RUNNING before ``/readyz`` (which runs the agent) responds,
    so we retry up to ``attempts`` times with ``delay_s`` between — but a
    *parseable* response (ready OR degraded) returns immediately; only an
    unreachable / unparseable endpoint triggers a retry. On total failure to
    reach it, returns ``(False, {"error": "<short>"})``.

    Best-effort and self-contained: never raises — the CALLER decides whether
    a not-ready result should fail the deploy. The bearer token is NEVER
    logged or echoed.
    """
    import json as _json
    import time as _time
    import urllib.error
    import urllib.request

    # Mint a bearer token through the established CLI seam. Never log it.
    token: str | None = None
    try:
        tok_proc = _run_databricks_cmd(["auth", "token", "--output", "json"], profile=profile)
        if getattr(tok_proc, "returncode", 1) == 0 and tok_proc.stdout:
            token = (_json.loads(tok_proc.stdout) or {}).get("access_token")
    except Exception as exc:  # pragma: no cover — defensive
        return False, {"error": f"could not mint token: {str(exc)[:120]}"}
    if not token:
        return False, {"error": "could not mint databricks auth token"}

    url = app_url.rstrip("/") + "/readyz"
    last_error = "unreachable"
    for attempt in range(max(1, attempts)):
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.HTTPError as http_err:
            # 503 (degraded) and friends arrive as HTTPError but still carry a
            # JSON body that is readable off the error object itself.
            try:
                raw = http_err.read()
            except Exception:
                raw = b""
        except Exception as exc:
            last_error = str(exc)[:120]
            raw = b""

        if raw:
            try:
                data = _json.loads(raw)
            except Exception:
                data = None
            if isinstance(data, dict) and "status" in data:
                checks = data.get("checks")
                if not isinstance(checks, dict):
                    checks = {k: v for k, v in data.items() if k != "checks"}
                return data.get("status") == "ready", checks

        # Unreachable / unparseable — back off and retry.
        if attempt < max(1, attempts) - 1 and delay_s > 0:
            _time.sleep(delay_s)

    return False, {"error": f"readyz unreachable: {last_error}"}


def _preflight_databricks_cli() -> None:
    """Block deploy early if the Databricks CLI isn't installed."""
    from . import _doctor as _d

    result = _d.check_databricks_cli()
    if result.status is not _d.Status.OK:
        raise click.ClickException(
            _fix_msg(
                "`apx-agent deploy` needs the Databricks CLI.",
                result.detail,
                result.fix,
            )
        )


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
            f"directory: {', '.join(missing)}. Run `apx-agent scaffold <name> "
            "--target apps` to generate the expected layout."
        )


def _validate_responses_agent_compiler() -> None:
    """Confirm mlflow.genai.agent_server is installed.

    The Apps deploy flow relies on ``compile_to_responses_agent`` at the
    deployed image level; the module-level import of ``_responses_agent``
    always succeeds (mlflow is lazy-imported). Check the real runtime dep
    (mlflow.genai) directly so a missing ``eval`` extra is caught at
    ``apx-agent deploy`` time rather than at first request.
    """
    try:
        import mlflow.genai.agent_server  # noqa: F401
    except ImportError as e:
        raise click.ClickException(
            "mlflow.genai.agent_server is not available — please install "
            "mlflow: pip install 'apx-agent[eval]' or pip install 'mlflow>=3.12'. "
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
        user_api_scopes_for,
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

    # Union the OBO scopes the agent's tools need onto the existing baseline
    # (the scaffold ships sql + serving; this adds dashboards.genie / vectorsearch
    # etc. when the agent actually uses those tools). Never drops existing scopes.
    derived_scopes = user_api_scopes_for(specs)
    existing_scopes = app_block.get("user_api_scopes") or []
    merged_scopes = sorted(set(existing_scopes) | set(derived_scopes))
    new_scopes = sorted(set(merged_scopes) - set(existing_scopes))
    if merged_scopes:
        app_block["user_api_scopes"] = merged_scopes

    path.write_text(yaml.safe_dump(doc, default_flow_style=False, sort_keys=False))

    if new_scopes:
        log(f"  added {len(new_scopes)} OBO scope(s): {', '.join(new_scopes)}")
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
    readyz_gate: bool = True,
) -> None:
    """Implement ``apx-agent deploy --target apps``.

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
            json_output=json_output, readyz_gate=readyz_gate, log=log,
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
    readyz_gate: bool = True,
    log: Any,
) -> None:
    """Inner body of ``_deploy_apps`` — see docstring there."""
    log(f"# apx-agent deploy --target apps (bundle-target={bundle_target}, "
        f"profile={profile or '<default>'})")

    # 1. Pre-flight
    _preflight_databricks_cli()
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
        agent = _load_finalized_agent(module)
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
        # Stage the dependency manifest into .build/. The artifacts script
        # omits pyproject.toml/uv.lock by design — without them the Apps
        # container has no manifest and falls back to base-image packages
        # (older mlflow, no mlflow.genai) → 502 (issue #116). Runs for BOTH the
        # wheel (editable/local) and git+https install shapes; passing the
        # wheel_path (possibly None) selects which.
        build_dir = cwd / ".build"
        if build_dir.is_dir():
            _stage_build_manifest(build_dir, wheel_path)
            # Fail loud at deploy time rather than as a runtime 502 if the
            # manifest somehow didn't land.
            if not (build_dir / "pyproject.toml").exists():
                raise click.ClickException(
                    "deploy aborted: .build/pyproject.toml is missing after "
                    "staging — the Apps container would have no dependency "
                    "manifest and 502 on startup. Check that the project root "
                    "has a pyproject.toml."
                )
    else:
        log("# --no-auto-build-wheel: skipping wheel build + artifacts step")

    # 2c. Auto-resolve mlflow_experiment_id if the bundle wants it and the
    # caller didn't pass one. Looks up / creates an experiment at
    # /Users/<current-user>/<bundle_name>-<target>.
    extra_vars: list[str] = []
    # Track the experiment id we end up deploying with, from either the
    # caller's --var or our auto-create. Used post-poll to grant the app SP
    # access so tracing can land.
    resolved_exp_id: str | None = None
    for v in vars or ():
        if v.startswith("mlflow_experiment_id="):
            resolved_exp_id = v.split("=", 1)[1].strip() or None
            break
    if auto_experiment and resolved_exp_id is None:
        eid = _ensure_experiment_id(
            profile=profile,
            bundle_name=app_name,
            bundle_target=bundle_target,
            env_value=None,
        )
        if eid:
            resolved_exp_id = eid
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

    # 6b. Grant the app's service principal access to the tracing experiment.
    # The experiment is created under the deploying user, but the app runs as
    # its SP — without this grant every span is dropped. Best-effort.
    sp = payload.get("service_principal_client_id")
    if sp and resolved_exp_id:
        if _grant_experiment_to_sp(resolved_exp_id, sp, profile=profile):
            log("  granted app SP CAN_MANAGE on tracing experiment")

    # 6c. readyz gate: prove the app actually answers + traces, not just that
    # the container booted. A green deploy should mean "the agent works".
    if readyz_gate and app_url:
        log(f"# readyz gate: GET {app_url}/readyz")
        ok, checks = _check_readyz(app_url, profile=profile)
        if ok:
            log(f"  readyz: ready ({checks})")
        else:
            # Parse checks dict into human-readable lines so the error
            # names the failing capability rather than dumping raw JSON.
            if isinstance(checks, dict):
                failing = [
                    f"  • {k}: {v}"
                    for k, v in checks.items()
                    if v not in ("ok", None, True)
                ]
                detail = "\n".join(failing) if failing else str(checks)
            else:
                detail = str(checks)
            raise click.ClickException(
                f"readyz gate failed — app is running but not healthy:\n{detail}\n"
                f"Fix the issues above then re-deploy, or re-run with --no-readyz-gate to skip."
            )

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

    agent = _load_finalized_agent(module)
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

    agent = _load_finalized_agent(module)
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
      apx-agent logs --endpoint NAME              Runtime logs from a Model Serving endpoint
      apx-agent logs --endpoint NAME --build      Build-time logs from the endpoint's build
      apx-agent logs --app NAME [--profile P]     Logs from an apx-agent hosted as a Databricks App

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

    # Finalize so `apx-agent info` reports the same tools/resources the serve and
    # deploy paths will — config-declared [[tool.apx.tools]] included.
    agent = _load_finalized_agent(module)

    # Walk the whole tree — HandoffAgent / SequentialAgent / etc. have no
    # _tool_fns of their own; the tools live on nested LlmAgents.
    tool_fns = list(_iter_tool_fns(agent))
    sub_agents = list(_iter_sub_agents(agent))
    instructions = getattr(agent, "_instructions", None) or ""

    tools_info: list[dict[str, Any]] = []
    for fn in tool_fns:
        meta = get_tool_metadata(fn)
        doc_lines = (fn.__doc__ or "").strip().splitlines()
        tools_info.append({
            "name": fn.__name__,
            "doc": doc_lines[0] if doc_lines else "",
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
            "apx-agent trace requires mlflow. Install with: pip install 'apx-agent[eval]'"
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
    are reported but don't fail. Pair with ``apx-agent test`` (smoke) and
    ``apx-agent eval`` (behavior) for a full pre-deploy check.
    """
    from ._lint import Severity, lint_agent

    agent = _load_finalized_agent(module)

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
            click.echo("apx-agent lint: clean — no findings.")
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
    or canary soak environments (apps), use `apx-agent canary` instead.
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

    click.echo(f"apx-agent hot-swap: {result.endpoint_name}")
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

    click.echo(f"apx-agent hot-swap --target apps: {result.app_name}")
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
    accept a message and return something?" — cheaper than apx-agent eval,
    no MLflow / eval dataset required.
    """
    agent = _load_finalized_agent(module)

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
            "apx-agent test requires the eval extra (mlflow). "
            "Install with: pip install 'apx-agent[eval]'"
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
            preview_lines = (text or "").strip().splitlines()
            preview = preview_lines[0] if preview_lines else "(empty)"
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


@main.group("list", invoke_without_command=True)
@click.option("--catalog", default=None,
              help="Restrict to a UC catalog. Default: any.")
@click.option("--schema", default=None,
              help="Restrict to a UC schema (requires --catalog).")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
@click.pass_context
def list_group(
    ctx: click.Context,
    catalog: str | None,
    schema: str | None,
    fmt: str,
) -> None:
    """Discover workspace resources — catalogs, schemas, tables, tools, agents.

    With no subcommand, behaves like ``apx-agent list agents`` for backwards
    compatibility. Run ``apx-agent list --help`` to see available subcommands.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_agents_cmd, catalog=catalog, schema=schema, fmt=fmt)


def _require_sdk() -> "Any":
    try:
        from databricks.sdk import WorkspaceClient
        return WorkspaceClient()
    except ImportError as e:
        raise click.ClickException("apx-agent list requires databricks-sdk.") from e


@list_group.command("agents")
@click.option("--catalog", default=None,
              help="Restrict to a UC catalog. Default: any.")
@click.option("--schema", default=None,
              help="Restrict to a UC schema (requires --catalog).")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def list_agents_cmd(catalog: str | None, schema: str | None, fmt: str) -> None:
    """Discover apx-agents in the workspace by their UC tags.

    Looks for registered models tagged ``apx.agent.name`` — the tag
    ``set_uc_tags_for_agent`` writes after deploy. Prints (name, model,
    endpoint hint, resource count). Useful for fleet operators
    inventorying multi-agent workspaces.
    """
    if schema and not catalog:
        raise click.UsageError("--schema requires --catalog.")

    ws = _require_sdk()

    try:
        models_iter = ws.registered_models.list(
            catalog_name=catalog,
            schema_name=schema,
            include_browse=False,
        )
        models = list(models_iter)
    except TypeError:
        models = list(ws.registered_models.list())  # type: ignore[call-arg]

    rows: list[dict[str, Any]] = []
    for m in models:
        tags = {t.key: t.value for t in (getattr(m, "tags", None) or [])}
        if "apx.agent.name" not in tags:
            continue
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


@list_group.command("catalogs")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def list_catalogs_cmd(fmt: str) -> None:
    """List Unity Catalog catalogs accessible in this workspace."""
    ws = _require_sdk()
    catalogs = _ws_list_catalogs(ws)

    if fmt == "json":
        click.echo(json.dumps(catalogs))
        return

    if not catalogs:
        click.echo("No catalogs found (or insufficient permissions).")
        return
    for name in catalogs:
        click.echo(name)


@list_group.command("schemas")
@click.argument("catalog")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def list_schemas_cmd(catalog: str, fmt: str) -> None:
    """List schemas in CATALOG."""
    ws = _require_sdk()
    schemas = _ws_list_schemas(ws, catalog)

    if fmt == "json":
        click.echo(json.dumps(schemas))
        return

    if not schemas:
        click.echo(f"No schemas found in {catalog}.")
        return
    for name in schemas:
        click.echo(name)


@list_group.command("tables")
@click.argument("catalog")
@click.argument("schema")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def list_tables_cmd(catalog: str, schema: str, fmt: str) -> None:
    """List tables in CATALOG.SCHEMA."""
    ws = _require_sdk()
    tables = _ws_list_tables(ws, catalog, schema)

    if fmt == "json":
        rows = [
            {
                "name": getattr(t, "name", None),
                "full_name": getattr(t, "full_name", None),
                "table_type": str(getattr(t, "table_type", "")),
                "comment": getattr(t, "comment", None),
            }
            for t in tables
        ]
        click.echo(json.dumps(rows, indent=2, default=str))
        return

    if not tables:
        click.echo(f"No tables found in {catalog}.{schema}.")
        return
    click.echo(f"{'TABLE':<40}  {'TYPE':<16}  COMMENT")
    for t in tables:
        name = getattr(t, "name", "-") or "-"
        ttype = str(getattr(t, "table_type", "")).replace("TableType.", "") or "-"
        comment = (getattr(t, "comment", None) or "")[:60]
        click.echo(f"{name:<40}  {ttype:<16}  {comment}")


@list_group.command("tools")
@click.argument("catalog")
@click.argument("schema")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def list_tools_cmd(catalog: str, schema: str, fmt: str) -> None:
    """List UC functions in CATALOG.SCHEMA (available as agent tools).

    These are the functions ``apx-agent publish-tools`` registers and that
    DataAgent / CoworkerAgent can call when ``include_functions=True``.
    """
    ws = _require_sdk()
    fns = _ws_list_functions(ws, catalog, schema)

    if fmt == "json":
        rows = [
            {
                "name": getattr(f, "name", None),
                "full_name": getattr(f, "full_name", None),
                "comment": getattr(f, "comment", None),
            }
            for f in fns
        ]
        click.echo(json.dumps(rows, indent=2, default=str))
        return

    if not fns:
        click.echo(f"No UC functions found in {catalog}.{schema}.")
        return
    click.echo(f"{'FUNCTION':<50}  COMMENT")
    for f in fns:
        name = getattr(f, "name", "-") or "-"
        comment = (getattr(f, "comment", None) or "")[:70]
        click.echo(f"{name:<50}  {comment}")


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
        raise click.ClickException("apx-agent cost requires databricks-sdk.") from e

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
        raise click.ClickException("apx-agent topology requires databricks-sdk.") from e

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
    agent = _load_finalized_agent(module)

    # Load the evalset same way apx-agent eval does
    data: Any
    path = Path(evalset)
    try:
        if path.suffix.lower() == ".jsonl":
            data = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        elif path.suffix.lower() == ".json":
            data = json.loads(path.read_text())
        else:
            data = evalset
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Failed to parse evalset {evalset}: {e}") from e

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
    import re

    parts = table.split(".")
    if len(parts) != 3 or not all(re.fullmatch(r"[A-Za-z0-9_]+", p) for p in parts):
        raise click.UsageError(
            "--table must be a three-part UC name (catalog.schema.table) "
            f"of bare identifiers [A-Za-z0-9_]; got {table!r}"
        )
    # Backtick-quote each identifier part so it can never break out of the
    # FROM clause even though the parts are already allowlist-validated.
    quoted_table = ".".join(f"`{p}`" for p in parts)

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
        f"FROM {quoted_table} "
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

    click.echo("# watchdog status"
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
# Both subcommand groups (`apx-agent memory ...` and `apx-agent examples ...`) operate
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
