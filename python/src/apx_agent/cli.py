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

Most agent-facing commands accept ``--module MODULE:VAR`` to point at the
agent definition (defaults to ``agent:agent``); the module must be importable
from the current working directory. The OKF bundle commands
(``refresh-schema`` / ``migrate-to-okf`` / ``pull-comments``) and
``scaffold`` / ``cost`` operate on the project, not a loaded agent, and so
take no ``--module``.

The CLI is a thin orchestration layer over the primitives — every command
maps to a single ``apx_agent`` call. The CLI's value isn't logic; it's
ergonomics. Implementation should stay narrow.
"""

from __future__ import annotations

import contextlib
import difflib
import importlib
import importlib.metadata
import json
import logging
import os
import re
import sys
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, NoReturn, cast

import click

if TYPE_CHECKING:
    from ._models import AgentConfig

# Suppress noisy third-party deprecation warnings that users can't act on.
warnings.filterwarnings(
    "ignore",
    message="The default value of `allowed_objects` will change",
    category=DeprecationWarning,
)
warnings.filterwarnings(
    "ignore",
    message="The default value of `allowed_objects` will change",
    category=PendingDeprecationWarning,
)

from . import _doctor as _doctor_mod
from ._schema import introspect_schema_columns, APX_DIR

logger = logging.getLogger(__name__)


class _ModuleSpec(NamedTuple):
    module_path: str
    variable: str


class _AppNameResolution(NamedTuple):
    bundle_key: str
    app_name: str


class _ReadyzResult(NamedTuple):
    is_ready: bool
    checks: dict[str, Any]


class _BundleUpdateResult(NamedTuple):
    added: list[str]
    skipped: list[str]


class _SplitOnboardingResponse(NamedTuple):
    """The two fenced blocks extracted from an onboarding LLM response."""
    plan_markdown: str | None
    spec_toml: str | None


def _split_onboarding_response(text: str) -> _SplitOnboardingResponse:
    """Extract the ```markdown and ```toml fenced blocks from an LLM response.

    Non-greedy regex stops at the first closing fence, so the prompt
    instructs the LLM not to nest triple-backtick fences inside the markdown
    plan content itself.
    """
    plan_match = re.search(r"```markdown\s*\n(.*?)```", text, re.DOTALL)
    toml_match = re.search(r"```toml\s*\n(.*?)```", text, re.DOTALL)
    return _SplitOnboardingResponse(
        plan_markdown=plan_match.group(1).strip() if plan_match else None,
        spec_toml=toml_match.group(1).strip() if toml_match else None,
    )


def _validate_spec_toml(toml_text: str) -> str | None:
    """Return None if toml_text parses and matches CoworkerTemplate.Spec,
    else a human-readable error string."""
    import tomllib

    from apx_agent.coworker import CoworkerTemplate

    try:
        parsed = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as exc:
        return f"TOML parse error: {exc}"
    try:
        CoworkerTemplate.Spec(**parsed)
    except Exception as exc:
        return f"Spec validation error: {exc}"
    return None


class _EnvMergeResult(NamedTuple):
    """Outcome of merging --env / --secret-env into databricks.yml."""
    env_added: list[str]
    secret_added: list[str]
    skipped: list[str]


class _EnvPair(NamedTuple):
    """A parsed ``--env KEY=VALUE`` pair."""
    name: str
    value: str


class _SecretEnvRef(NamedTuple):
    """A parsed ``--secret-env KEY=scope/key`` reference (never a value)."""
    name: str
    scope: str
    key: str


class _AdvertiseResult(NamedTuple):
    """Outcome of advertising an agent — the resolved workspace client plus the
    endpoint/description the discovery registry recorded, consumed together by
    the advertise/publish commands."""
    ws: Any
    endpoint: str
    description: str


def _fix_msg(title: str, detail: str, fix: str | None) -> str:
    """Consistent error body for hardened CLI failures."""
    parts = [title, detail]
    if fix:
        parts.append(f"\nFix:\n    {fix}")
    parts.append("\nRun `uv run apx-agent doctor` for a full check.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Agent loader
# ---------------------------------------------------------------------------


def _parse_module_spec(spec: str) -> _ModuleSpec:
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
    return _ModuleSpec(module_path=module_path, variable=variable)


def _read_apx_agent_config(pyproject_path: Path | None = None) -> dict[str, Any]:
    """Read ``[tool.apx.agent]`` from ``pyproject.toml`` in cwd.

    Also merges personal overrides from ``.apx.local`` (a gitignored TOML
    file in the project root). Keys in ``.apx.local`` take precedence —
    this is where user-specific values like ``profile`` live so they are
    never committed to version control.

    Returns an empty dict if the file is missing, malformed, or doesn't
    have the section.
    """
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]

    def _read_toml_section(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
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
        return agent_cfg if isinstance(agent_cfg, dict) else {}

    cwd = (pyproject_path.parent if pyproject_path else Path.cwd())
    committed = _read_toml_section(pyproject_path or cwd / "pyproject.toml")

    # .apx.local uses the same [tool.apx.agent] structure but is gitignored.
    local_overrides = _read_toml_section(cwd / ".apx.local")

    return {**committed, **local_overrides}


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
    from ._wiring import finalize_agent, resolve_agent, _ws_for_template, TemplateConfigError
    from ._inspection import _load_agent_config

    config = _load_agent_config(pyproject_path=None)
    try:
        agent = resolve_agent(module_spec, config, ws=_ws_for_template(config))
    except TemplateConfigError as e:
        raise click.ClickException(
            f"{e}\n"
            "Run this command from inside a scaffolded project directory, "
            "or pass --module to point at your agent file."
        ) from e
    except ImportError as e:
        raise click.ClickException(
            f"Failed to import agent module: {e}\n"
            "Check that your agent file has no syntax errors and all imports are available."
        ) from e
    finalize_agent(agent, config, pyproject_path=None)
    return agent


# ASGI app module spec served by `apx-agent run`, per scaffold layout.
_RUN_MODULE_BY_TARGET = {
    "model-serving": "app:app",
    "apps": "agent_server.start_server:app",
}

# Default foundation model for the Apps runtime when APX_MODEL is unset. Matches
# the value baked into the scaffold's app.yaml; override via the APX_MODEL env var.
_APPS_DEFAULT_MODEL = "databricks-claude-sonnet-4-6"


class _DetectedTarget(NamedTuple):
    target: str
    reason: str


def _detect_target(cwd: Path | None = None) -> _DetectedTarget:
    """Infer the project's target (model-serving vs apps) from its layout.

    Rules, in order: apps when ``agent_server/start_server.py`` exists;
    model-serving when a top-level ``app.py`` exists without ``databricks.yml``;
    otherwise apps — the catch-all default (covers ``databricks.yml``-only and
    bare layouts). ``reason`` names the marker that decided, so callers can
    echo why a target was auto-detected. Pass ``--target`` to override.
    """
    cwd = cwd or Path.cwd()
    if (cwd / "agent_server" / "start_server.py").exists():
        return _DetectedTarget("apps", "agent_server/start_server.py present")
    if (cwd / "app.py").exists() and not (cwd / "databricks.yml").exists():
        return _DetectedTarget("model-serving", "app.py present without databricks.yml")
    if (cwd / "databricks.yml").exists():
        return _DetectedTarget("apps", "databricks.yml present")
    return _DetectedTarget("apps", "no layout markers; apps is the default")


def _detect_module_spec(cwd: Path | None = None) -> str | None:
    """Infer the agent module spec from the project layout.

    Returns the most likely ``module:variable`` string for ``_load_finalized_agent``,
    or ``None`` when no agent file is found. Used by commands that load the agent
    but where ``--module`` is optional (e.g. ``publish``).
    """
    cwd = cwd or Path.cwd()
    candidates = [
        ("agent.py", "agent:agent"),
        ("app.py", "app:agent"),
        ("agent_server/agent.py", "agent_server.agent:agent"),
    ]
    for filename, spec in candidates:
        if (cwd / filename).exists():
            return spec
    return None


def _sanitize_uv_lock(lock_path: Path) -> bool:
    """Re-point a uv.lock's internal Databricks PyPI proxy at public PyPI.

    The dev's ``UV_INDEX_URL`` / ``~/.config/uv/uv.toml`` leaks
    ``pypi-proxy.dev.databricks.com`` into the lock. Deployed apps / serving
    endpoints (and external users) can't reach that host, so any lock we ship
    must point at public hosts. Two things get rewritten:

    1. the index registry line (``.../simple`` → ``pypi.org/simple``), and
    2. per-package download URLs — this proxy also *serves package files*
       through itself (``.../packages/...``), which a deployed container cannot
       reach. The proxy mirrors PyPI's path layout, so those re-point cleanly at
       ``files.pythonhosted.org/packages/...``. (Without this, the index line
       was clean but every wheel URL still pointed at the unreachable proxy and
       the app's package install failed.)

    Returns True if the file changed.
    """
    if not lock_path.exists():
        return False
    text = lock_path.read_text()
    original = text
    text = text.replace(
        "https://pypi-proxy.dev.databricks.com/simple",
        "https://pypi.org/simple",
    )
    text = text.replace(
        "https://pypi-proxy.dev.databricks.com/packages/",
        "https://files.pythonhosted.org/packages/",
    )
    if text == original:
        return False
    lock_path.write_text(text)
    return True


def _warn_unknown_lock_mirrors(lock_path: Path) -> None:
    """Warn about non-public package hosts ``_sanitize_uv_lock`` can't rewrite.

    The sanitizer only knows the Databricks dev proxy; any other internal
    mirror (Artifactory, a prod proxy variant) survives it, and the deployed
    container's ``uv sync`` will likely fail on it (#416). Warn — don't block —
    since the mirror may be reachable from the target environment.
    """
    if not lock_path.exists():
        return
    hosts = _doctor_mod.nonpublic_lock_hosts(lock_path.read_text())
    if not hosts:
        return
    click.echo(
        f"# WARNING: {lock_path.name} still resolves packages from non-public "
        f"host(s): {', '.join(hosts)}. There is no rewrite rule for them, so "
        "the deployed container's `uv sync` will likely fail. Re-point the "
        "lock at public PyPI manually (unset custom index env vars, then "
        "`uv lock`).",
        err=True,
    )


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


def _save_to_pyproject(key: str, value: str, *, local: bool = False) -> bool:
    """Write ``key = "<value>"`` into ``[tool.apx.agent]``.

    When ``local=True``, writes to ``.apx.local`` (gitignored) instead of
    ``pyproject.toml``. Use ``local=True`` for personal settings like
    ``profile`` that should not be committed to version control.

    Returns True on success.
    """
    if local:
        target = Path.cwd() / ".apx.local"
        if not target.exists():
            # Bootstrap a minimal .apx.local with the required section.
            target.write_text("[tool.apx.agent]\n")
        content = target.read_text()
        if "[tool.apx.agent]" not in content:
            content += "\n[tool.apx.agent]\n"
    else:
        target = Path.cwd() / "pyproject.toml"
        if not target.exists():
            return False
        content = target.read_text()
        if "[tool.apx.agent]" not in content:
            return False

    escaped = value.replace('"', '\\"')
    if re.search(rf'^\s*{re.escape(key)}\s*=', content, re.MULTILINE):
        content = re.sub(
            rf'^\s*{re.escape(key)}\s*=.*$',
            f'{key} = "{escaped}"',
            content,
            flags=re.MULTILINE,
        )
    else:
        content = re.sub(
            r'(\[tool\.apx\.agent\])',
            f'[tool.apx.agent]\n{key} = "{escaped}"',
            content,
        )
    target.write_text(content)
    return True


def _save_profile_to_pyproject(profile: str) -> bool:
    return _save_to_pyproject("profile", profile, local=True)


def _interactive_pick_profile() -> str | None:
    """Prompt the user to pick a ~/.databrickscfg profile, set it for this
    session, and offer to persist it to .apx.local. Returns the chosen profile
    name, or None when no profiles exist / not a TTY. Caller re-checks auth."""
    import sys
    if not sys.stdin.isatty():
        return None
    profiles = _list_databricks_profiles()
    if not profiles:
        return None
    click.echo("Databricks auth unresolved — pick a profile to use for this session:\n")
    for i, (name, host, valid) in enumerate(profiles, 1):
        status = "✓" if valid else " "
        short_host = host.replace("https://", "").split(".")[0] if host else ""
        click.echo(f"  {i:2}. {status} {name:<28} {short_host}")
    default = next((i for i, (_, _, v) in enumerate(profiles, 1) if v), 1)
    idx = click.prompt("\nChoose", type=click.IntRange(1, len(profiles)), default=default)
    chosen, _, _ = profiles[idx - 1]
    os.environ["DATABRICKS_CONFIG_PROFILE"] = chosen
    if _save_profile_to_pyproject(chosen):
        click.echo(f"  Saved profile '{chosen}' to .apx.local — future runs will use it automatically.\n")
    else:
        click.echo(f"  Using profile '{chosen}' for this session (export DATABRICKS_CONFIG_PROFILE={chosen} to make it permanent).\n")
    return chosen




def _preflight_databricks_auth() -> None:
    """Fail `apx-agent run`/`deploy` with dev-time guidance when auth is unresolved.

    Reads ``profile`` from ``[tool.apx.agent]`` in the local pyproject.toml
    and applies it before the auth check so saved preferences are honoured
    automatically. When running in a TTY and auth still fails, shows a profile
    picker and offers to save the choice back to pyproject.toml.
    """
    from . import _doctor as _d

    # Apply a saved profile preference before running the auth check.
    if "DATABRICKS_CONFIG_PROFILE" not in os.environ:
        saved_profile = _read_apx_agent_config().get("profile")
        if saved_profile and isinstance(saved_profile, str):
            os.environ["DATABRICKS_CONFIG_PROFILE"] = saved_profile

    result = _d.check_databricks_auth()
    if result.status is _d.Status.FAIL:
        # Auth is ambiguous or unresolved — let the user pick a profile
        # interactively instead of just printing instructions and exiting.
        if _interactive_pick_profile():
            result = _d.check_databricks_auth()  # retry with the chosen profile

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


_MOVED_COMMANDS: dict[str, str] = {
    "scaffold":         "apx-agent agents scaffold",
    "run":              "apx-agent agents run",
    "stop":             "apx-agent agents stop",
    "deploy":           "apx-agent agents deploy",
    "apps":             "apx-agent agents apps",
    "info":             "apx-agent agents describe",
    "logs":             "apx-agent agents logs",
    "cost":             "apx-agent agents cost",
    "hot-swap":         "apx-agent agents hot-swap",
    "refresh-schema":   "apx-agent agents refresh-schema",
    "create-supervisor":"apx-agent agents create-supervisor",
    "list":             "apx-agent agents list  (or  apx-agent uc catalogs/schemas/tables/tools)",
    "trace":            "apx-agent traces list",
    "export-traces":    "apx-agent traces export",
    "eval-chain":       "apx-agent eval chain",
    "lint":             "apx-agent eval lint",
    "test":             "apx-agent eval test",
    "topology":         "apx-agent uc topology",
    "publish-tools":    "apx-agent uc publish",
    "mcp-config":       "apx-agent uc mcp-config",
    "publish":          "apx-agent agents publish",
}


class _ApxCommand(click.Command):
    """click.Command that auto-adds a universal ``--json`` alias.

    Any command exposing ``--format`` with ``json`` among its choices gets a
    ``--json`` shorthand (an alias writing to the same ``fmt`` dest via
    ``flag_value="json"``), so ``--json`` is THE consistent way to ask any
    apx-agent command for machine-readable output — without editing each
    handler. Commands whose ``--format`` has no ``json`` choice (e.g. a
    mermaid/graphviz diagram format) are left untouched.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        fmt = next(
            (p for p in self.params
             if isinstance(p, click.Option) and p.name == "fmt"
             and "json" in (getattr(p.type, "choices", None) or ())),
            None,
        )
        already = any("--json" in p.opts for p in self.params)
        if fmt is not None and not already:
            self.params.append(click.Option(
                ["--json", "fmt"], flag_value="json",
                help="Shorthand for --format json.",
            ))


class _ApxGroup(click.Group):
    """click.Group that suggests the closest command on a typo, and gives a
    clear redirect for commands that moved to a sub-group."""

    # Every leaf command in an apx group gets the --json alias (see _ApxCommand).
    command_class = _ApxCommand

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            cmd_name = args[0] if args else ""
            if cmd_name in _MOVED_COMMANDS:
                raise click.UsageError(
                    f"'{cmd_name}' moved.  Use:  {_MOVED_COMMANDS[cmd_name]}"
                ) from None
            matches = difflib.get_close_matches(
                cmd_name, self.list_commands(ctx), n=1
            )
            hint = f" Did you mean `{matches[0]}`?" if matches else ""
            raise click.UsageError(f"No such command '{cmd_name}'.{hint}") from None


def _stdio_is_interactive() -> bool:
    """True when both stdin and stdout are terminals — bare ``apx-agent`` drops
    into the shell only then, so scripts/CI keep the safe help-on-no-args
    behaviour (and never hang a REPL on a non-interactive stdin)."""
    import sys

    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


@click.group(cls=_ApxGroup, invoke_without_command=True)
@click.version_option(_resolve_version(), package_name="apx-agent", prog_name="apx")
@click.pass_context
def main(ctx: click.Context) -> None:
    """apx-agent — declarative agents on Databricks. See `apx-agent --help` for commands.

    Run with no command in a terminal to drop into the interactive shell.
    """
    if ctx.invoked_subcommand is not None:
        return
    # No subcommand: a terminal user gets the interactive shell; everything else
    # (pipes, scripts, CI) gets help, exactly as before.
    if _stdio_is_interactive():
        from ._shell import run_shell

        run_shell()
    else:
        click.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# Sub-groups
# ---------------------------------------------------------------------------


@main.group(cls=_ApxGroup)
def agents() -> None:
    """Create, run, deploy, and manage agents."""


@main.group(cls=_ApxGroup)
def traces() -> None:
    """Inspect and export MLflow traces."""


@main.group("eval", cls=_ApxGroup)
def eval_group() -> None:
    """Evaluate agent quality — run evals, chain-evals, lint, and test."""


@main.group(cls=_ApxGroup)
@click.option(
    "--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
    help="Databricks config profile (~/.databrickscfg).",
)
@click.pass_context
def uc(ctx: click.Context, profile: str | None) -> None:
    """Browse and publish Unity Catalog resources."""
    ctx.ensure_object(dict)
    ctx.obj["profile"] = profile


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


def _describe_param(param: click.Parameter) -> dict[str, Any]:
    """One option/argument as a JSON-serializable descriptor."""
    ptype = getattr(param, "type", None)
    type_name = getattr(ptype, "name", "text") if ptype is not None else "text"
    default = param.default
    # Keep defaults JSON-safe — callables / sentinels become their repr.
    if callable(default) or not isinstance(default, (str, int, float, bool, type(None))):
        default = None if default is None else repr(default)
    out: dict[str, Any] = {
        "name": param.name,
        "kind": "argument" if isinstance(param, click.Argument) else "option",
        "flags": list(param.opts),
        "type": type_name,
        "required": bool(param.required),
        "is_flag": bool(getattr(param, "is_flag", False)),
        "default": default,
        "help": getattr(param, "help", None),
    }
    choices = getattr(ptype, "choices", None)
    if type_name == "choice" and choices:
        out["choices"] = list(choices)
    envvar = getattr(param, "envvar", None)
    if envvar:
        out["envvar"] = envvar
    return out


def _describe_command(cmd: click.Command, ctx: click.Context) -> dict[str, Any]:
    """Recursively describe a click Command/Group as a JSON tree."""
    node: dict[str, Any] = {"help": cmd.get_short_help_str(limit=200) or None}
    params = [p for p in cmd.params if not (isinstance(p, click.Option) and p.name == "help")]
    if params:
        node["params"] = [_describe_param(p) for p in params]
    if isinstance(cmd, click.Group):
        sub: dict[str, Any] = {}
        for name in sorted(cmd.list_commands(ctx)):
            child = cmd.get_command(ctx, name)
            if child is not None and not child.hidden:
                sub[name] = _describe_command(child, ctx)
        node["commands"] = sub
    return node


@main.command("describe-cli")
def describe_cli() -> None:
    """Emit the entire CLI command tree as JSON — every command, flag, type,
    default, and one-line help in one document.

    A machine-readable manifest so an AI agent (or any tool) can discover the
    full surface in a single call instead of crawling `--help` screens.
    """
    ctx = click.Context(main, info_name="apx")
    tree = _describe_command(main, ctx)
    tree["name"] = "apx"
    tree["version"] = _resolve_version()
    click.echo(json.dumps(tree, indent=2))


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
    # Honour a saved profile preference, then run once. If Databricks auth is
    # the only thing blocking and we're interactive, let the user pick a profile
    # and re-run — so `doctor` fixes the common "ambiguous profile" case in place.
    if "DATABRICKS_CONFIG_PROFILE" not in os.environ:
        saved = _read_apx_agent_config().get("profile")
        if isinstance(saved, str) and saved:
            os.environ["DATABRICKS_CONFIG_PROFILE"] = saved

    groups = _doctor_mod.run_checks(Path.cwd(), online=not offline)
    auth_failed = any(
        c.name == "Databricks auth" and c.status is _doctor_mod.Status.FAIL
        for _g, cs in groups for c in cs
    )
    if auth_failed and _interactive_pick_profile():
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
# status
# ---------------------------------------------------------------------------


def _resolve_active_profile() -> str | None:
    """The Databricks profile apx-agent would use, resolved offline.

    Mirrors the precedence the commands apply: explicit env var first, then a
    saved ``.apx.local`` preference. Returns None when neither is set (the SDK
    falls back to DEFAULT). No network call — safe to call from a shell prompt.
    """
    env = os.environ.get("DATABRICKS_CONFIG_PROFILE")
    if env:
        return env
    saved = _read_apx_agent_config().get("profile")
    return saved if isinstance(saved, str) and saved else None


def _status_prompt_string(cwd: Path) -> str:
    """Compact ``apx:name(target) ▸ profile`` context string for a shell prompt.

    Offline and never raises — a broken prompt is worse than an empty one.
    Shared by ``status --prompt`` and the interactive ``apx-agent shell``.
    """
    try:
        from . import _doctor as _d

        in_project = _d._is_apx_project(cwd)
        profile = _resolve_active_profile()
        name = _read_apx_agent_config().get("name") if in_project else None
        target = _detect_target(cwd).target if in_project else None
        parts = []
        if in_project:
            parts.append(f"apx:{name}({target})" if name else f"apx({target})")
        else:
            parts.append("apx")
        if profile:
            parts.append(f"▸ {profile}")
        return " ".join(parts)
    except Exception:
        return "apx"


@main.command()
@click.option(
    "--prompt", "as_prompt", is_flag=True,
    help="Emit a compact one-line summary for a shell prompt (offline, never errors).",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit a machine-readable orientation payload (profile, project, declared "
         "agents, suggested next commands) — one call for an AI agent to get its bearings.",
)
def status(as_prompt: bool, as_json: bool) -> None:
    """Show the active profile and project context — offline, no API call.

    Run on demand to confirm what you're pointed at before deploying. Pass
    --prompt for a compact one-liner you can wire into your shell prompt, or
    --json for a full orientation payload (what's declared here + next steps).
    """
    from . import _doctor as _d

    cwd = Path.cwd()
    in_project = _d._is_apx_project(cwd)
    profile = _resolve_active_profile()
    name = _read_apx_agent_config().get("name") if in_project else None
    target = _detect_target(cwd).target if in_project else None

    if as_json:
        agents_here = _discover_local_agents(cwd) if in_project else []
        if not in_project:
            nxt = ["apx-agent agents scaffold <NAME>   # create a new agent project"]
        else:
            nxt = [
                "apx-agent agents list --local   # agents declared here (offline)",
                "apx-agent agents describe        # introspect the agent's tools/resources",
                "apx-agent doctor                 # full environment + auth check",
                "apx-agent agents deploy          # deploy to the workspace",
            ]
        payload: dict[str, Any] = {
            "profile": profile,
            "in_project": in_project,
            "cwd": str(cwd),
            "project": None if not in_project else {
                "name": name,
                "target": target,
                "agents": agents_here,
            },
            "next": nxt,
        }
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    if as_prompt:
        click.echo(_status_prompt_string(cwd))
        return

    click.echo(f"profile: {profile or 'DEFAULT (unset — may be ambiguous)'}")
    if in_project:
        click.echo(f"project: {name or '(unnamed)'}")
        click.echo(f"target:  {target}")
    else:
        click.echo(f"project: none ({cwd} is not an apx project)")
    click.echo("\nRun `apx-agent doctor` for a full environment check.")


@main.command()
def shell() -> None:
    """Interactive REPL — be *in* apx, not firing one-off commands.

    Run any apx-agent command without the prefix (``status``, ``agents list
    --local``, ``doctor`` …), with an ambient context prompt (project ▸
    profile), tab-completion of the whole command tree, history, and ``cd`` to
    move between projects. Ctrl-D or ``exit`` to leave.
    """
    from ._shell import run_shell

    run_shell()


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
{knowledge_line}
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
<KNOWLEDGE_LINE># ADK-style layout: the user's agent definition lives at top-level
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

import os

from apx_agent import DataAgent

<EXAMPLE_TOOL>
# A governed data agent over ``<CATALOG>.<SCHEMA>`` (schema baked at scaffold
# time — no runtime discovery needed). It answers questions grounded in that
# schema and queries via a SQL tool that runs as the calling user.
#
# Make it your own:
#   * point it at your own data:  re-scaffold with --catalog/--schema
#   * re-scaffold to refresh the baked schema:  apx-agent scaffold <name> --catalog <c>
#   * add ``genie_space=...`` / ``vector_index=...`` for Genie or Vector Search
#   * or drop back to a plain ``Agent(tools=[...])`` with your own ``@tool``s
#
# APX_CATALOG/APX_SCHEMA (set per DAB target — see README "Promoting to
# another environment") override the scaffolded default below, so the same
# agent.py can run against a different catalog/schema per environment.
_CATALOG = "<CATALOG>"
_SCHEMA = "<SCHEMA>"

agent = DataAgent(
    os.environ.get("APX_CATALOG", _CATALOG),
    os.environ.get("APX_SCHEMA", _SCHEMA)<EXTRA_TOOLS>,
    name="<APP_NAME>"<INSTRUCTIONS_ARG>,
)
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
    instructions=<INSTRUCTIONS_VALUE>,
    name="<APP_NAME>",
)
'''


_SCAFFOLD_APPS_AGENT_COWORKER = '''\
from __future__ import annotations

import os

from apx_agent import CoworkerAgent

<EXAMPLE_TOOL>
# APX_CATALOG/APX_SCHEMA (set per DAB target — see README "Promoting to
# another environment") override the scaffolded default below, so the same
# agent.py can run against a different catalog/schema per environment.
_CATALOG = "<CATALOG>"
_SCHEMA = "<SCHEMA>"

# Add memory="persistent" to remember facts and context across sessions.
agent = CoworkerAgent(
    os.environ.get("APX_CATALOG", _CATALOG),
    os.environ.get("APX_SCHEMA", _SCHEMA)<EXTRA_TOOLS><PERSONA_ARG><JOIN_KEY_ARG><OBJECTIVE_ARG>,
    name="<APP_NAME>"<INSTRUCTIONS_ARG>,
)
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
from apx_agent._memory_wiring import resolve_conversation_store
from apx_agent._mlflow_tracing import autolog_if_env
from apx_agent._wiring import finalize_agent

# Enable MLflow LangChain/LangGraph auto-tracing when APX_AGENT_MLFLOW_AUTOLOG
# is set (databricks.yml sets it on deploy). Must run before the agent's
# LangChain components are built/invoked.
autolog_if_env()

# Import the user's agent from the top-level module.
from agent import agent

MODEL = os.environ.get("APX_MODEL", "databricks-claude-sonnet-4-6")

_ws = _make_workspace_client()
# Load pyproject.toml config and fully finalize the agent so [tool.apx.agent]
# blocks (memory, session, tools, guardrails) are honoured on Apps deploy —
# not just when served via create_app().
_agent_config = _load_agent_config()
finalize_agent(agent, _agent_config, ws=_ws)
# Conversation store: config-driven (Lakebase via [tool.apx.agent].session)
# when declared; otherwise an in-process store so multi-turn threading and the
# dev UI History panel work out of the box. In-memory state resets on restart —
# declare a session block for durable history.
_conversation_store = resolve_conversation_store(_agent_config, _ws, agent=agent)
if _conversation_store is None:
    from apx_agent import InMemoryConversationStore
    _conversation_store = InMemoryConversationStore()
# agent_id must match the name the dev-UI History panel filters by
# (the pyproject [tool.apx.agent].name) or conversations stay invisible.
_invoke_fn, _stream_fn = compile_to_responses_agent(
    agent,
    model=MODEL,
    conversation_store=_conversation_store,
    agent_id=_agent_config.name if _agent_config else None,
)


# Async bridge: AgentServer calls sync handlers directly ON the event
# loop, so a slow tool would block every other request on the replica
# (other sessions, the dev UI, /health). These wrappers run the sync
# agent work on worker threads instead — see apx_agent._async_bridge.
from apx_agent import make_async_invoke, make_async_stream

non_streaming = invoke()(make_async_invoke(_invoke_fn))
streaming = stream()(make_async_stream(_stream_fn))


server = AgentServer(agent_type="ResponsesAgent")
app = server.app

mount_mcp_endpoints(app, agent)
mount_readyz(app, agent)

app.state.workspace_client = _ws
app.state.conversation_store = _conversation_store


if __name__ == "__main__":
    server.run("agent_server.start_server:app")
'''


_SCAFFOLD_APPS_QUICKSTART = '''\
"""quickstart — one-shot setup for the <APP_NAME> Apps deploy.

Creates the MLflow experiment for tracing and writes its ID to .env.
Reports Lakebase memory/session backend status if configured.
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
# Uncomment to enable persistent memory on Lakebase. Set the LAKEBASE_HOST env
# var to point at an existing Lakebase database (tables auto-create on first use).
# [tool.apx.agent.memory]
# type = "lakebase"
# host = "${LAKEBASE_HOST}"
# database = "<APP_NAME_SLUG>"
# table_name = "<CATALOG>.<SCHEMA>.apx_<APP_NAME_SLUG>_memory"
# embedding_model = "databricks-bge-large-en"
# embedding_dim = 1024
'''

# Default session backend: durable history + keyed state on Databricks Lakebase.
# `host` is the Lakebase endpoint DNS (a project endpoint or instance DNS); set the
# LAKEBASE_HOST env var in your app to point at an existing Lakebase database. Until
# it's set, sessions fall back to non-durable in-memory. The per-agent tables
# auto-create on first use. Pass --no-lakebase at scaffold time (or delete this
# block) to opt out.
_SCAFFOLD_SESSION_LAKEBASE_BLOCK = '''
[tool.apx.agent.session]
type = "lakebase"
host = "${LAKEBASE_HOST}"
database = "<APP_NAME_SLUG>"
'''

# Spliced into databricks.yml's `variables:`/`config.env:` blocks only when
# the scaffold actually has a catalog/schema (#323) — a base LlmAgent
# scaffold gets no dead config. <CATALOG>/<SCHEMA> inside these blocks are
# resolved by _scaffold_apps's `_sub()`, same as everywhere else in the
# template — see the CATALOG_SCHEMA_*_BLOCK replace calls added there.
_SCAFFOLD_CATALOG_SCHEMA_VARS_BLOCK = '''\
  catalog:
    description: |
      Unity Catalog catalog this agent is grounded in. Override per
      environment via `targets.<name>.variables.catalog` in this file —
      see README "Promoting to another environment".
    default: <CATALOG>
  schema:
    description: |
      Unity Catalog schema this agent is grounded in. Override per
      environment via `targets.<name>.variables.schema` in this file —
      see README "Promoting to another environment".
    default: <SCHEMA>
'''

_SCAFFOLD_CATALOG_SCHEMA_ENV_BLOCK = '''\
          - name: APX_CATALOG
            value: ${var.catalog}
          - name: APX_SCHEMA
            value: ${var.schema}
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
  apx_git_sha:
    description: |
      Git commit the deploy was cut from. Injected automatically by
      `apx-agent deploy --target apps` so every trace the app emits carries
      an apx.git_sha tag and `canary analyze` can attribute traffic per
      version (issue #404). Leave the default.
    default: ""
  apx_model_version:
    description: |
      UC registered-model version this app serves, when known BEFORE the
      deploy (e.g. a promote re-deploy: --var apx_model_version=7). The
      first deploy of a version registers UC AFTER the app is live, so this
      is usually empty and apx.git_sha is the correlation key.
    default: ""
<CATALOG_SCHEMA_VARS_BLOCK>

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
      cp -r .apx .build/ 2>/dev/null || true
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
          - name: APX_GIT_SHA
            value: ${var.apx_git_sha}
          - name: APX_MODEL_VERSION
            value: ${var.apx_model_version}
<CATALOG_SCHEMA_ENV_BLOCK>

targets:
  dev:
    mode: development
    default: true
  staging:
    mode: production
    resources:
      apps:
        <APP_NAME>:
          name: <APP_NAME>
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
uv run apx-agent deploy --target apps  # validates, deploys, runs the bundle
```

## Promoting to another environment
`databricks.yml` ships `dev` (default), `staging`, and `prod` targets. All
three default to the same workspace/catalog/schema until you customize one —
promoting is an explicit edit, not a hidden step.

The `catalog`/`schema` override below only applies to scaffolds with a data
source (`--template data`/`coworker`) — a base `LlmAgent` scaffold has no
`catalog`/`schema` variables to override, so it promotes via
`--bundle-target`/`--profile` alone.

To point `staging` at its own UC catalog/schema, add a `variables:` override
under its target in `databricks.yml`:

```yaml
targets:
  staging:
    mode: production
    variables:
      catalog: <your-staging-catalog>
      schema: <your-staging-schema>
    resources:
      apps:
        <APP_NAME>:
          name: <APP_NAME>
```

Then deploy to the staging workspace by pointing `--profile` at its
Databricks CLI profile:

```bash
uv run apx-agent deploy --target apps --bundle-target staging --profile <staging-profile>
```

Repeat the same recipe for `prod` (its own `variables:` override + its own
`--profile`). `--bundle-target` selects which `databricks.yml` target to
deploy; `--profile` selects which workspace credentials to use — they're
independent, so forgetting to change `--profile` when you add a `staging`
override just redeploys to the same workspace under a different catalog.

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


def _ws_is_connected(ws) -> bool:
    """Lightweight liveness check — returns True if the workspace client can talk to the API."""
    try:
        ws.current_user.me()
        return True
    except Exception:
        return False


def _echo_lakebase_guidance() -> None:
    """Tell the user how to make the durable Lakebase session store live.

    The scaffold points ``host`` at the ``LAKEBASE_HOST`` env var. Until that's
    set to an existing Lakebase endpoint, sessions run non-durable in-memory —
    say so, and never fail the scaffold.
    """
    click.echo()
    click.echo("Sessions: durable on Lakebase once you point host at an endpoint.")
    click.echo(
        "      Set LAKEBASE_HOST to your Lakebase database's endpoint DNS "
        "(project endpoint or instance DNS)."
    )
    click.echo("      Per-agent tables auto-create on first use; until then, sessions are in-memory.")
    click.echo("      (--no-lakebase next time to omit the session block entirely)")


def _list_databricks_profiles() -> list[tuple[str, str, bool]]:
    """Return (name, host, valid) for each profile via 'databricks auth profiles'.

    Falls back to parsing ~/.databrickscfg (name only, valid=unknown) if the
    CLI is unavailable.
    """
    import subprocess as _sp
    try:
        out = _sp.run(
            ["databricks", "auth", "profiles"],
            capture_output=True, text=True, timeout=15, check=False,
        ).stdout
        results = []
        for line in out.splitlines():
            parts = line.split()
            # Output columns: Name  Host  Valid  (header row has no URL)
            if len(parts) >= 3 and parts[-1] in ("YES", "NO") and parts[1].startswith("http"):
                results.append((parts[0], parts[1], parts[-1] == "YES"))
        if results:
            return results
    except Exception:
        pass
    # Fallback: parse ~/.databrickscfg directly
    import configparser
    cfg_path = Path.home() / ".databrickscfg"
    if not cfg_path.exists():
        return []
    try:
        c = configparser.ConfigParser()
        c.read(cfg_path)
        return [(s, c.get(s, "host", fallback=""), False)
                for s in c.sections() if not s.startswith("__")]
    except Exception:
        return []


def _gate_workspace_for_scaffold(profile: str | None):
    """Ensure we have a live workspace connection before catalog/schema prompts.

    If no connection is found, lists existing ~/.databrickscfg profiles to pick
    from, or offers to run ``databricks configure`` in-place. Loops until
    connected or Ctrl-C. Returns a verified WorkspaceClient.
    """
    import subprocess as _sp

    from databricks.sdk import WorkspaceClient

    def _try_connect(p: str | None, timeout: int = 10):
        import concurrent.futures

        def _do():
            try:
                ws = WorkspaceClient(profile=p) if p else WorkspaceClient()
                me = ws.current_user.me()
                return ws, me.user_name or ws.config.host
            except Exception as exc:
                return None, str(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_do)
            try:
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                return None, f"timed out after {timeout}s — host may be unreachable"

    click.echo("  Checking workspace connection…", nl=False)
    ws, info = _try_connect(profile)
    if ws is not None:
        click.echo(f"\r  Connected as {info}                    ")
        return ws
    click.echo(f"\r  No connection ({info})")

    click.echo(
        "\nNo Databricks workspace connection found"
        + (f" (profile: {profile!r})" if profile else "")
        + " — need to authenticate before choosing a catalog.\n"
    )

    while True:
        profiles = _list_databricks_profiles()
        if profiles:
            click.echo("Available Databricks profiles:")
            for i, (name, host, valid) in enumerate(profiles, 1):
                status = "✓ valid" if valid else "  expired"
                short_host = host.replace("https://", "").split(".")[0] if host else ""
                click.echo(f"  {i:2}. {name:<28} {status}  {short_host}")
            new_idx = len(profiles) + 1
            click.echo(f"  {new_idx:2}. Add / refresh a profile  (databricks auth login)")
            default = next((i for i, (_, _, v) in enumerate(profiles, 1) if v), 1)
            idx = click.prompt(
                "Choose",
                type=click.IntRange(1, new_idx),
                default=default,
            )
            if idx < new_idx:
                chosen, host, valid = profiles[idx - 1]
                if not valid:
                    click.echo(f"  Profile '{chosen}' is expired — refreshing via 'databricks auth login --profile {chosen}'")
                    _sp.run(["databricks", "auth", "login", "--profile", chosen], check=False)
                    click.echo()
                click.echo(f"  Connecting with profile '{chosen}'…", nl=False)
                ws, info = _try_connect(chosen)
                if ws is not None:
                    click.echo(f"\r  Connected as {info}                    ")
                    return ws
                click.echo(f"\r  Still couldn't connect with '{chosen}': {info}")
                click.echo()
                continue
        else:
            click.echo("  No profiles found.")

        # Add / refresh path
        click.echo()
        _sp.run(["databricks", "auth", "login"], check=False)
        click.echo()
        ws, info = _try_connect(None)
        if ws is not None:
            click.echo(f"  Connected as {info}")
            return ws
        click.echo("  Still no connection — try selecting a different profile.")


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


def _bake_schema_into_project(
    project_dir: Path, config: Any, profile: str | None
) -> bool:
    """Write ``.apx/schema.json`` into a generated project so a YAML-spec
    deploy/run is grounded (the DataAgent declares its UC tables and skips
    runtime ``DESCRIBE``; the dev-UI DATA panel shows the tables).

    ``generate_project`` intentionally writes no ``.apx/``, so without this the
    YAML path runs ungrounded. Best-effort: returns False (and the agent falls
    back to live introspection) when the template has no resolved catalog/schema
    or the schema can't be read. The bundle step copies ``.apx/`` into ``.build/``.
    """
    template = config.template if isinstance(config.template, dict) else None
    if not template:
        return False
    catalog = template.get("catalog")
    schema = template.get("schema")
    if not (isinstance(catalog, str) and isinstance(schema, str)):
        return False
    if "$" in catalog or "$" in schema:  # unresolved $CATALOG/$SCHEMA placeholders
        return False
    manifest = _schema_manifest_for_scaffold(catalog, schema, profile)
    if manifest is None:
        return False
    from ._okf import dump_schema_cache

    apx_dir = project_dir / ".apx"
    apx_dir.mkdir(parents=True, exist_ok=True)
    (apx_dir / "schema.json").write_text(dump_schema_cache(manifest))
    return True


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


def _pick_from_list(items: list[str], prompt: str) -> str:
    """Present a numbered list and return the user's selection."""
    if not items:
        raise click.ClickException("No items found in the workspace.")
    for i, item in enumerate(items, 1):
        click.echo(f"  {i:2}. {item}")
    raw = click.prompt(prompt, default="1")
    try:
        idx = int(raw) - 1
        if not 0 <= idx < len(items):
            raise ValueError
    except ValueError:
        raise click.ClickException(f"Invalid selection: {raw!r}")
    return items[idx]


_ONBOARDING_GEN_PROMPT = """\
You are helping a Databricks Field Engineer onboard a brand-new non-profit \
organization that has no existing Databricks presence. You will be given \
answers from a short interview with the organization. Produce TWO outputs, \
each in its own fenced code block, in this exact order:

1. A ```markdown``` block containing a PHASED EXECUTION PLAN for onboarding \
   this organization onto apx-agent. Propose the specific phases based on \
   their answers, but include AT LEAST:
   - A phase for landing their existing data into Unity Catalog (referencing \
     whatever current tools they named — Kiva, Blackbaud, spreadsheets, etc.).
   - A phase for standing up a CoworkerAgent grounded in that landed data.
   Each phase should have a short name, a one-paragraph objective, and a \
   rough effort estimate (e.g. "1-2 weeks"). Do NOT use triple-backtick code \
   fences inside this markdown content itself.

2. A ```toml``` block containing a DRAFT CoworkerTemplate.Spec config with \
   EXACTLY these keys (flat TOML key = value pairs, no nesting, no tables):

   catalog = "TBD-catalog"
   schema = "TBD-schema"
   persona = "<a one-line analyst persona based on their primary stakeholder>"
   join_key = "<a plausible join key field name>"
   objective = "<2-3 sentences on what this coworker should surface, from their pain point>"
   memory = "persistent"

   catalog and schema MUST be exactly the literal placeholder strings above \
   — no UC data has been landed yet, and phase 1 of the plan is deciding \
   where it will go. Do not invent real-looking catalog/schema names.

Here is what the organization said:
{spec}

Output ONLY the two fenced blocks (```markdown ... ``` then ```toml ... ```). \
No other text before, between, or after them.
"""


def _query_onboarding_llm(ws: "Any", prompt_text: str) -> str:
    """One LLM call for onboarding generation; raises ClickException on failure."""
    from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

    try:
        response = ws.serving_endpoints.query(
            name="databricks-claude-sonnet-4-6",
            messages=[ChatMessage(role=ChatMessageRole.USER, content=prompt_text)],
            max_tokens=2000,
        )
        choices = response.choices
        if not choices or choices[0].message is None or choices[0].message.content is None:
            raise click.ClickException("LLM returned an empty response.")
        return choices[0].message.content.strip()
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(
            f"LLM generation failed: {exc}\n"
            "Ensure you have a valid Databricks profile with access to "
            "databricks-claude-sonnet-4-6."
        ) from exc


def _onboarding_response_error(split: "_SplitOnboardingResponse", *, retry: bool) -> "str | None":
    """Human-readable validation error for a parsed onboarding response, or
    None if both artifacts are usable. The plan markdown must be non-blank
    (it has no syntax to fail, but the LLM can still omit it) and the TOML
    spec must parse and validate — a missing/invalid EITHER artifact triggers
    the same one-retry recovery path in _generate_onboarding_plan."""
    source = "retry response" if retry else "response"
    if not (split.plan_markdown or "").strip():
        return f"no ```markdown block found in the {source}"
    if split.spec_toml is None:
        return f"no ```toml block found in the {source}"
    return _validate_spec_toml(split.spec_toml)


class _OnboardingPlan(NamedTuple):
    """The full result of one apx-agent onboard interview + generation pass."""
    org_name: str
    plan_markdown: str
    spec_toml: str
    validation_error: "str | None"


def _generate_onboarding_plan(profile: "str | None") -> _OnboardingPlan:
    """Interview the user about a new non-profit, then generate a phased plan
    + a draft CoworkerTemplate.Spec block, validating the latter with one retry."""
    click.echo("\nLet's learn about your organization. Answer a few questions:\n")
    org_name = click.prompt("Organization name")
    mission = click.prompt("What does your organization do? (one or two sentences)")
    current_tools = click.prompt(
        "What tools do you currently use to run day-to-day operations? "
        "(e.g. Kiva, Blackbaud, spreadsheets, QuickBooks, something else)"
    )
    pain_point = click.prompt("What's the most painful, manual part of your day-to-day work?")
    unanswerable_question = click.prompt(
        "What's a question you wish you could answer instantly but can't today?"
    )
    record_scale = click.prompt(
        "Roughly how many records do you track — donors, loans, beneficiaries, "
        "whatever's core to your work?"
    )
    stakeholder = click.prompt(
        "Who's the primary stakeholder who'd use this system day-to-day? "
        "(executive director, ops lead, program manager, etc.)"
    )

    spec_context = (
        f"Organization name: {org_name}\n"
        f"What they do: {mission}\n"
        f"Current tools: {current_tools}\n"
        f"Biggest pain point: {pain_point}\n"
        f"Question they wish they could answer: {unanswerable_question}\n"
        f"Rough record scale: {record_scale}\n"
        f"Primary stakeholder: {stakeholder}"
    )

    click.echo("\nGenerating onboarding plan...\n")
    from databricks.sdk import WorkspaceClient

    try:
        ws = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
    except Exception as exc:
        raise click.ClickException(
            f"Could not connect to the workspace: {exc}\n"
            "Ensure you have a valid Databricks profile with access to "
            "databricks-claude-sonnet-4-6."
        ) from exc

    prompt_text = _ONBOARDING_GEN_PROMPT.format(spec=spec_context)
    response_text = _query_onboarding_llm(ws, prompt_text)
    split = _split_onboarding_response(response_text)
    error = _onboarding_response_error(split, retry=False)

    if error is not None:
        retry_prompt = (
            f"{prompt_text}\n\n"
            f"Your previous response's TOML block failed validation with this "
            f"error: {error}\n\n"
            "Return the full response again (both fenced blocks, in the same "
            "order), fixing the TOML block so it parses and matches the "
            "required fields exactly."
        )
        response_text = _query_onboarding_llm(ws, retry_prompt)
        split = _split_onboarding_response(response_text)
        error = _onboarding_response_error(split, retry=True)

    return _OnboardingPlan(
        org_name=org_name,
        plan_markdown=split.plan_markdown or "",
        spec_toml=split.spec_toml or "",
        validation_error=error,
    )


_COWORKER_GEN_PROMPT = """\
You are an expert at designing Databricks coworker agents. Generate a YAML spec for a coworker
agent that joins two enterprise data systems and surfaces actionable insights.

Use EXACTLY this structure (no extra keys, no markdown fences):

name: <kebab-case-name>
description: >
  <1-2 sentence description ending with a period.>
model: databricks-claude-sonnet-4-6
instructions: >
  You are <persona>. Always cite the <join_key> when surfacing discrepancies.
  <2-3 sentences about how to distinguish error types and what to prioritise.>
examples:
  - "<example question 1>"
  - "<example question 2>"
  - "<example question 3>"
  - "<example question 4>"

template:
  name: coworker
  catalog: $CATALOG
  schema: $SCHEMA
  persona: <persona one-liner>
  join_key: <join key field name>
  objective: >
    <2-3 sentences about the core reconciliation objective.>
  memory: persistent
  warehouse_id: $WAREHOUSE_ID
  include_functions: true

memory:
  type: lakebase
  host: $LAKEBASE_HOST
  database: <name_underscored>
  table_name: $CATALOG.$SCHEMA.apx_<name_underscored>_memory
  embedding_model: databricks-bge-large-en
  embedding_dim: 1024
  auto_create: true
  validate_at_boot: true

session:
  type: lakebase
  host: $LAKEBASE_HOST
  database: <name_underscored>
  table_name: $CATALOG.$SCHEMA.apx_<name_underscored>_sessions
  auto_create: true
  validate_at_boot: true

guardrails:
  injection_detection: true
  blocked_tools: []
  rate_limit: null

tools: []

Here is the user's coworker definition:
{spec}

Output ONLY the YAML. No explanation, no markdown fences.
"""


def _generate_coworker_yaml(profile: "str | None") -> str:
    """Ask the user a few questions then use an LLM to author the full coworker YAML."""
    click.echo("\nLet's define your coworker. Answer a few questions:\n")
    system_a = click.prompt("System A (e.g. Salesforce, SAP, Oracle ERP)")
    system_a_data = click.prompt(f"What data lives in {system_a}? (e.g. closed deals, invoices)")
    system_b = click.prompt("System B (e.g. NetSuite, Workday, ServiceNow)")
    system_b_data = click.prompt(f"What data lives in {system_b}? (e.g. payments, HR records)")
    join_key = click.prompt("Join key between the two systems (e.g. account_id, employee_id)")
    persona = click.prompt("Analyst persona (e.g. a revenue operations analyst)")
    objective = click.prompt("What should the coworker surface? (e.g. unbilled deals, pay discrepancies)")

    spec = (
        f"System A: {system_a} — {system_a_data}\n"
        f"System B: {system_b} — {system_b_data}\n"
        f"Join key: {join_key}\n"
        f"Persona: {persona}\n"
        f"Objective: {objective}"
    )
    click.echo("\nGenerating coworker YAML...\n")
    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.service.serving import ChatMessage, ChatMessageRole
        ws = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
        response = ws.serving_endpoints.query(
            name="databricks-claude-sonnet-4-6",
            messages=[
                ChatMessage(
                    role=ChatMessageRole.USER,
                    content=_COWORKER_GEN_PROMPT.format(spec=spec),
                )
            ],
            max_tokens=1200,
        )
        choices = response.choices
        if not choices or choices[0].message is None or choices[0].message.content is None:
            raise click.ClickException("LLM returned an empty response.")
        return choices[0].message.content.strip()
    except Exception as exc:
        raise click.ClickException(
            f"LLM generation failed: {exc}\n"
            "Ensure you have a valid Databricks profile with access to "
            "databricks-claude-sonnet-4-6, or pick from the gallery with --coworker list."
        ) from exc


def _pick_coworker_gallery() -> "tuple[str, Path]":
    """List bundled coworker gallery YAMLs and return (name, path) of the selection."""
    import importlib.resources as _ir
    gallery_dir = Path(_ir.files("apx_agent").joinpath("coworker_gallery"))  # type: ignore[arg-type]
    yamls = sorted(gallery_dir.glob("*.yaml"))
    if not yamls:
        raise click.ClickException("No coworker gallery files found.")
    import yaml as _yaml
    entries: list[tuple[str, str, Path]] = []
    for p in yamls:
        try:
            data = _yaml.safe_load(p.read_text())
            cname = data.get("name", p.stem)
            desc = str(data.get("description", "")).strip().splitlines()[0]
        except Exception:
            cname, desc = p.stem, ""
        entries.append((cname, desc, p))
    click.echo("\nAvailable coworker templates:")
    for i, (cname, desc, _) in enumerate(entries, 1):
        click.echo(f"  {i:2}. {cname:<35} {desc}")
    raw = click.prompt("Select coworker", default="1")
    try:
        idx = int(raw) - 1
        if not 0 <= idx < len(entries):
            raise ValueError
    except ValueError:
        raise click.ClickException(f"Invalid selection: {raw!r}")
    cname, _, path = entries[idx]
    return cname, path


@main.command()
@click.option(
    "--profile", default=None,
    help="Databricks CLI profile for the LLM call. Falls back to $DATABRICKS_CONFIG_PROFILE.",
)
@click.option(
    "--dir", "directory",
    default=".",
    type=click.Path(file_okay=False),
    help="Directory to write the plan + spec files into. Default: current directory.",
)
@click.option("--force", is_flag=True, help="Overwrite existing output files.")
def onboard(profile: "str | None", directory: str, force: bool) -> None:
    """Interview a new non-profit org and generate a phased onboarding plan.

    Asks a few plain-language business questions, then writes:

    \b
    - <org-slug>-onboarding-plan.md — a human-readable phased execution plan.
    - <org-slug>-coworker.toml — a draft CoworkerTemplate.Spec block (written
      as <org-slug>-coworker.DRAFT.toml instead if it fails validation).

    Next step after review: apx-agent agents scaffold --coworker <path-to-toml>.
    """
    from apx_agent._publish import _slug

    result = _generate_onboarding_plan(profile)
    if not result.plan_markdown.strip():
        raise click.ClickException(
            "LLM returned no usable onboarding plan (even after retry). Re-run."
        )
    slug = _slug(result.org_name)
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)

    plan_path = out_dir / f"{slug}-onboarding-plan.md"
    if plan_path.exists() and not force:
        raise click.ClickException(f"{plan_path} already exists. Pass --force to overwrite.")
    plan_path.write_text(result.plan_markdown)
    click.echo(f"\nWrote {plan_path}")

    if result.validation_error is None:
        spec_path = out_dir / f"{slug}-coworker.toml"
    else:
        spec_path = out_dir / f"{slug}-coworker.DRAFT.toml"
    if spec_path.exists() and not force:
        raise click.ClickException(f"{spec_path} already exists. Pass --force to overwrite.")

    spec_content = result.spec_toml
    if result.validation_error is not None:
        spec_content = (
            f"# UNVALIDATED — generated spec failed validation: {result.validation_error}\n"
            "# Review and fix before use.\n\n" + spec_content
        )
    spec_path.write_text(spec_content)

    if result.validation_error is None:
        click.echo(f"Wrote {spec_path}")
        click.echo(f"\nNext step: apx-agent agents scaffold --coworker {spec_path}")
    else:
        click.echo(
            f"Wrote {spec_path} (UNVALIDATED — {result.validation_error})\n"
            "Review and fix the spec block before using it."
        )


def _pick_template() -> str:
    """List registered templates and return the user's selection."""
    from ._template import template_registry
    templates = template_registry.list()
    if not templates:
        raise click.ClickException("No templates registered.")
    click.echo("\nAvailable templates:")
    for i, t in enumerate(templates, 1):
        click.echo(f"  {i:2}. {t.name:<12} {t.title} — {t.description}")
    raw = click.prompt("Select template", default="1")
    try:
        idx = int(raw) - 1
        if not 0 <= idx < len(templates):
            raise ValueError
    except ValueError:
        raise click.ClickException(f"Invalid selection: {raw!r}")
    return templates[idx].name


def _scaffold_sanity_check(
    ws: "Any | None",
    template: str,
    catalog: str | None,
    schema: str | None,
) -> None:
    """Warn about common mistakes without blocking the user."""
    needs_data = template in ("data", "coworker")
    if needs_data and not catalog:
        click.echo("  Warning: --catalog is not set. The agent will have no data source.", err=True)
        return
    if needs_data and catalog == "hive_metastore":
        click.echo(
            "  Warning: hive_metastore is a legacy catalog. "
            "Consider migrating your tables to Unity Catalog for full governance.",
            err=True,
        )
    if needs_data and catalog and schema and ws is not None:
        tables = _ws_list_tables(ws, catalog, schema)
        if not tables:
            click.echo(
                f"  Warning: no tables found in {catalog}.{schema}. "
                "The agent will be grounded on an empty schema — add data first.",
                err=True,
            )


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
        # Gate: require a live workspace connection before asking for catalog/schema.
        # We extract the profile from the ws config if one was already passed in.
        if ws is None or not _ws_is_connected(ws):
            existing_profile = getattr(getattr(ws, "config", None), "profile", None) if ws else None
            ws = _gate_workspace_for_scaffold(existing_profile)
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
            click.echo("  (no catalogs found in your workspace — enter a name manually)")
            while True:
                catalog = click.prompt("Catalog").strip()
                if catalog:
                    break
                click.echo("  Catalog cannot be empty.")

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
            click.echo(f"  (no schemas found in {catalog} — enter a name manually)")
            while True:
                schema = click.prompt(f"Schema").strip()
                if schema:
                    break
                click.echo("  Schema cannot be empty.")

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


def _write_okf_bundle_for_scaffold(target: Path, manifest: dict, *, force: bool) -> None:
    """Write the OKF bundle (source of truth) next to the derived schema.json cache."""
    from datetime import datetime, timezone
    from ._okf import write_okf_bundle

    okf_root = target / ".apx" / "okf"
    if okf_root.exists() and not force:
        click.echo(f"  skip   {okf_root} (exists; pass --force to overwrite)")
        return
    write_okf_bundle(manifest, okf_root, timestamp=datetime.now(timezone.utc).isoformat())
    click.echo(f"  write  {okf_root} (OKF bundle)")


def _scaffold_model_serving(
    target: Path, name: str, force: bool, catalog: str, schema: str,
    table: str | None = None,
) -> None:
    """Write the original Model Serving project layout into ``target``.

    Mirrors the pre-``--target`` shape: top-level ``agent.py`` + ``app.py``.
    """
    prelude, extra_tools = _example_tool_block(catalog, schema, table)

    manifest = _schema_manifest_for_scaffold(catalog, schema)
    knowledge_line = 'knowledge = "./.apx/okf"\n' if manifest is not None else ""
    files = {
        "pyproject.toml": _SCAFFOLD_PYPROJECT.format(name=name, knowledge_line=knowledge_line),
        "agent.py": _SCAFFOLD_AGENT.format(
            name=name, catalog=catalog, schema=schema,
            example_tool=prelude, extra_tools=extra_tools,
        ),
        "app.py": _SCAFFOLD_APP,
        ".gitignore": _SCAFFOLD_GITIGNORE,
        "README.md": _SCAFFOLD_README.format(name=name),
    }
    if manifest is not None:
        from ._okf import dump_schema_cache
        files[".apx/schema.json"] = dump_schema_cache(manifest)
    for rel_path, content in files.items():
        path = target / rel_path
        if path.exists() and not force:
            click.echo(f"  skip   {path} (exists; pass --force to overwrite)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        click.echo(f"  write  {path}")
    if manifest is not None:
        _write_okf_bundle_for_scaffold(target, manifest, force=force)


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
    join_key: str | None = None, lakebase: bool = True,
    instructions: str | None = None,
) -> None:
    """Write a Databricks Apps-ready project layout into ``target``.

    Produces the ``agent_server/`` + ``scripts/`` + ``databricks.yml``
    bundle shape consumed by ``databricks bundle deploy``.
    """
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
    instructions_arg = f", instructions={repr(instructions)}" if instructions else ""
    instructions_value = repr(instructions) if instructions else repr("You are a helpful assistant.")

    import re as _re
    name_slug = _re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "agent"

    # Emit the knowledge knob iff the bundle is actually being written (manifest is not None).
    # This keeps pyproject.toml coherent: the knob resolves iff the bundle exists on disk.
    knowledge_line = 'knowledge = "./.apx/okf"\n' if manifest is not None else ""

    # Splice UC catalog/schema as DAB variables only when this template
    # actually has a data source (#323) — a base LlmAgent scaffold has no
    # catalog/schema and would otherwise get dead config in databricks.yml.
    catalog_schema_vars_block = (
        _SCAFFOLD_CATALOG_SCHEMA_VARS_BLOCK if (catalog and schema) else ""
    )
    catalog_schema_env_block = (
        _SCAFFOLD_CATALOG_SCHEMA_ENV_BLOCK if (catalog and schema) else ""
    )

    def _sub(template: str) -> str:
        return (
            template.replace("<CATALOG_SCHEMA_VARS_BLOCK>\n", catalog_schema_vars_block)
            .replace("<CATALOG_SCHEMA_ENV_BLOCK>\n", catalog_schema_env_block)
            .replace("<APP_NAME>", name)
            .replace("<APP_NAME_SLUG>", name_slug)
            .replace("<CATALOG>", catalog)
            .replace("<SCHEMA>", schema)
            .replace("<EXAMPLE_TOOL>", prelude)
            .replace("<EXTRA_TOOLS>", extra_tools)
            .replace("<PERSONA_ARG>", persona_arg)
            .replace("<OBJECTIVE_ARG>", objective_arg)
            .replace("<JOIN_KEY_ARG>", join_key_arg)
            .replace("<INSTRUCTIONS_ARG>", instructions_arg)
            .replace("<INSTRUCTIONS_VALUE>", instructions_value)
            .replace("<APX_AGENT_DEP>", apx_dep)
            .replace("<APX_AGENT_SOURCE>", apx_source)
            .replace("<KNOWLEDGE_LINE>", knowledge_line)
        )

    # Enable a durable Lakebase session store by default (--no-lakebase opts out to
    # non-durable in-memory sessions). Session persistence needs only an instance +
    # database, so it's emitted regardless of whether a UC catalog/schema resolved.
    # The commented memory block is added only when a UC catalog/schema is known.
    session_block = _SCAFFOLD_SESSION_LAKEBASE_BLOCK if lakebase else ""
    memory_block = _SCAFFOLD_MEMORY_BLOCK if (catalog and schema) else ""
    pyproject = _sub(_SCAFFOLD_APPS_PYPROJECT + session_block + memory_block)

    files: dict[str, str] = {
        "pyproject.toml": pyproject,
        "databricks.yml": _sub(_SCAFFOLD_APPS_DATABRICKS_YML),
        ".env.example": _SCAFFOLD_APPS_ENV_EXAMPLE,
        ".gitignore": _SCAFFOLD_GITIGNORE,
        "README.md": _sub(_SCAFFOLD_APPS_README),
        # ADK-style layout: the user's agent definition lives at top-level
        # ``agent.py``. ``agent_server/`` is framework boilerplate only.
        "agent.py": (
            _sub(_SCAFFOLD_APPS_AGENT_BASE) if template == "base"
            else _sub(_SCAFFOLD_APPS_AGENT_COWORKER if template == "coworker" else _SCAFFOLD_APPS_AGENT)
        ),
        "agent_server/__init__.py": "",
        "agent_server/start_server.py": _SCAFFOLD_APPS_START_SERVER,
        "scripts/__init__.py": "",
        "scripts/quickstart.py": _sub(_SCAFFOLD_APPS_QUICKSTART),
    }
    if manifest is not None:
        from ._okf import dump_schema_cache
        files[".apx/schema.json"] = dump_schema_cache(manifest)
    for rel_path, content in files.items():
        path = target / rel_path
        if path.exists() and not force:
            click.echo(f"  skip   {path} (exists; pass --force to overwrite)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        click.echo(f"  write  {path}")
    if manifest is not None:
        _write_okf_bundle_for_scaffold(target, manifest, force=force)


def _materialize_agent(
    config: "AgentConfig", target: Path, *, force: bool,
    source_dir: Path | None = None,
) -> None:
    """Compile *config* into a durable, hand-editable project at *target*.

    Shared by ``scaffold``'s gallery pick and ``generate`` — both start from
    an arbitrary structured spec (a gallery YAML, an LLM-authored YAML) that
    ``_scaffold_apps``'s scalar templating can't represent. Wraps
    ``generate_project()`` (the compiler ``run``/``deploy`` already use for
    standalone ``.yaml`` specs) and adds the auxiliary files it doesn't write
    but ``_scaffold_apps`` does, so output quality doesn't depend on which
    authoring path produced the config.
    """
    from ._project_gen import generate_project

    if target.exists() and not force and any(target.iterdir()):
        raise click.ClickException(
            f"{target} already exists and is not empty. Pass --force to overwrite."
        )
    target.mkdir(parents=True, exist_ok=True)

    generate_project(config, target, source_dir=source_dir)

    aux_files = {
        ".env.example": _SCAFFOLD_APPS_ENV_EXAMPLE,
        ".gitignore": _SCAFFOLD_GITIGNORE,
        "README.md": _SCAFFOLD_APPS_README.replace("<APP_NAME>", config.name),
        "scripts/__init__.py": "",
        "scripts/quickstart.py": _SCAFFOLD_APPS_QUICKSTART.replace("<APP_NAME>", config.name),
    }
    for rel_path, content in aux_files.items():
        path = target / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or force:
            path.write_text(content)

    click.echo()
    click.echo(f"Scaffolded {config.name} at {target} (target=apps).")
    click.echo(f"Next: cd {target.name} && uv sync && uv run apx-agent run    # serve locally")
    click.echo("Tip: run `uv run apx-agent doctor` to check your environment before deploying.")
    click.echo("      uv run apx-agent deploy                  # → Databricks Apps")


def _echo_scaffold_yaml_done(out: Path, *, catalog: str | None, schema: str | None) -> None:
    """Print a consistent, correct post-YAML message and offer to run locally."""
    import subprocess as _sp
    import sys

    click.echo(f"\nSpec written to {out.name}")
    if not (catalog and schema):
        missing = " and ".join(
            v for v, flag in [("$CATALOG", catalog), ("$SCHEMA", schema)] if not flag
        )
        click.echo(f"  Open {out.name} and fill in {missing} before running.")
        click.echo(f"\n  apx-agent run {out.name}     # run locally")
        click.echo(f"  apx-agent deploy {out.name}  # deploy to Databricks Apps")
        return

    if sys.stdin.isatty():
        click.echo()
        launch = click.confirm("Start the local dev server now?", default=True)
        if launch:
            _sp.run(["apx-agent", "run", str(out)], check=False)
            return

    click.echo(f"\n  apx-agent run {out.name}     # run locally")
    click.echo(f"  apx-agent deploy {out.name}  # deploy to Databricks Apps")


def _scaffold_from_gallery(
    gallery_yaml_path: Path,
    name: str,
    directory: Path,
    catalog: "str | None",
    schema: "str | None",
) -> None:
    """Copy a gallery YAML to <directory>/<name>.yaml, patching name/catalog/schema."""
    import yaml as _yaml
    data = _yaml.safe_load(gallery_yaml_path.read_text())
    data["name"] = name
    if catalog and "template" in data:
        data["template"]["catalog"] = catalog
    if schema and "template" in data:
        data["template"]["schema"] = schema
    # warehouse_id is optional and auto-detected at runtime; remove the $VAR placeholder
    if "template" in data and "warehouse_id" in data["template"]:
        del data["template"]["warehouse_id"]
    # patch memory/session table names to use the agent name + resolved catalog/schema
    safe_name = name.replace("-", "_")
    cat = catalog or "$CATALOG"
    sch = schema or "$SCHEMA"
    for section in ("memory", "session"):
        if section in data and "table_name" in data[section]:
            suffix = "memory" if section == "memory" else "sessions"
            data[section]["table_name"] = f"{cat}.{sch}.apx_{safe_name}_{suffix}"
    out = directory / f"{name}.yaml"
    out.write_text(_yaml.dump(data, sort_keys=False, allow_unicode=True))
    _echo_scaffold_yaml_done(out, catalog=catalog, schema=schema)


def _prompt_for_instructions() -> str | None:
    """Ask what the agent should do. Returns None when left blank (fill later)."""
    raw = click.prompt(
        "\nWhat should this agent do?  (its instructions)\n"
        "  e.g. 'Answer questions about Salesforce opportunities and pipeline health.'\n"
        "  Leave blank to fill in the YAML later",
        default="",
        show_default=False,
    )
    return raw.strip() or None


def _scaffold_to_yaml(
    name: str,
    directory: Path,
    scaffold_template: str,
    catalog: str | None,
    schema: str | None,
    persona: str | None,
    join_key: str | None,
    objective: str | None,
    instructions: str | None = None,
) -> None:
    import yaml as _yaml
    spec: dict = {
        "name": name,
        "description": "",
        "model": "databricks-claude-sonnet-4-6",
        "instructions": instructions or "",
        "examples": [],
    }
    if scaffold_template in ("coworker", "data"):
        spec["template"] = {
            "name": scaffold_template,
            "catalog": catalog or "$CATALOG",
            "schema": schema or "$SCHEMA",
        }
        if scaffold_template == "coworker":
            spec["template"]["persona"] = persona or "a data analyst"
            spec["template"]["join_key"] = join_key or ""
            spec["template"]["objective"] = objective or ""
            spec["template"]["memory"] = "persistent"
            safe_name = name.replace("-", "_")
            cat = catalog or "$CATALOG"
            sch = schema or "$SCHEMA"
            spec["memory"] = {
                "type": "lakebase",
                "host": "${LAKEBASE_HOST}",
                "database": safe_name,
                "table_name": f"{cat}.{sch}.apx_{safe_name}_memory",
                "embedding_model": "databricks-bge-large-en",
                "embedding_dim": 1024,
                "auto_create": True,
            }
            spec["session"] = {
                "type": "lakebase",
                "host": "${LAKEBASE_HOST}",
                "database": safe_name,
                "table_name": f"{cat}.{sch}.apx_{safe_name}_sessions",
                "auto_create": True,
            }
    spec["guardrails"] = {"injection_detection": False}
    spec["tools"] = []
    out = directory / f"{name}.yaml"
    out.write_text(_yaml.dump(spec, sort_keys=False, allow_unicode=True))
    _echo_scaffold_yaml_done(out, catalog=catalog, schema=schema)


@agents.command()
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
    type=click.Choice(["base", "data", "coworker", "list"]),
    default=None,
    help=(
        "Agent kind: 'base', 'data', 'coworker', or 'list' to pick interactively. "
        "Shorthand: --coworker / --data."
    ),
)
@click.option(
    "--coworker", "coworker_spec", default=None, is_eager=False,
    metavar="[list|NAME]",
    help=(
        "Pick or generate a coworker. Use 'list' to browse the gallery, "
        "'generate' to LLM-author one from your description, "
        "a coworker name to select directly, or omit the value to use "
        "--template coworker."
    ),
)
@click.option("--data", "use_data", is_flag=True, default=False,
              help="Shorthand for --template data.")
@click.option(
    "--lakebase/--no-lakebase", "lakebase", default=True,
    help="Enable a durable Lakebase session store by default (shared 'apx-agent' "
    "instance, per-agent database, created by `uv run quickstart`). Use "
    "--no-lakebase for non-durable in-memory sessions.",
)
@click.option(
    "--interactive/--no-interactive", "interactive", default=None,
    help="Run the setup wizard (target, template, catalog, schema, persona). "
         "Defaults to on when stdin is a TTY.",
)
@click.option(
    "--yaml/--no-yaml", "emit_yaml", default=True,
    help="Output a YAML spec file instead of a full project directory "
         "(default: on — unless --target is passed explicitly, which "
         "scaffolds that runtime's project layout; pass --yaml to keep "
         "the spec output).",
)
def scaffold(
    name: str, directory: str, scaffold_target: str | None, force: bool, here: bool,
    catalog: str | None, schema: str | None, profile: str | None,
    scaffold_template: str | None, coworker_spec: str | None, use_data: bool,
    lakebase: bool, interactive: bool | None, emit_yaml: bool,
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
                f"Note: scaffolding inside the apx-agent repo → {new_target}  "
                f"(use --here to scaffold at the current location instead)",
                err=True,
            )
            target = new_target

    # ``name`` may be a path (e.g. "examples/my-agent"); the project/agent name
    # baked into the templates must be the bare directory name, or it produces
    # an invalid PEP 508 ``[project].name`` that breaks ``uv sync``.
    project_name = Path(name).name

    # Resolve shorthand template flags before any other logic.
    if coworker_spec is not None:
        scaffold_template = "coworker"
    elif use_data:
        scaffold_template = "data"

    # An explicit --target is a request for that runtime's documented project
    # layout (#449): the Apps agent_server/ bundle or the flat model-serving
    # project. Don't let the default-on --yaml spec flow silently reroute it
    # to a YAML spec — only an explicitly passed --yaml keeps the spec output.
    if scaffold_target is not None and emit_yaml:
        _yaml_source = click.get_current_context().get_parameter_source("emit_yaml")
        if _yaml_source is not click.core.ParameterSource.COMMANDLINE:
            emit_yaml = False

    if not emit_yaml:
        if target.exists() and not force:
            if any(target.iterdir()):
                raise click.ClickException(
                    f"{target} already exists and is not empty. Pass --force to overwrite."
                )
        target.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Step 0: template/catalog/schema pickers and sanity check.
    # Triggered by passing "list" as the value for any of these options.
    # -----------------------------------------------------------------------
    gallery_yaml_path: "Path | None" = None
    generated_yaml_str: "str | None" = None
    if coworker_spec is not None and coworker_spec not in ("",):
        if coworker_spec == "list":
            # --coworker list → interactive gallery picker
            _, gallery_yaml_path = _pick_coworker_gallery()
        elif coworker_spec == "generate":
            # --coworker generate → LLM-author from user's description
            generated_yaml_str = _generate_coworker_yaml(profile)
        else:
            # --coworker <name> → find by name in the gallery
            import importlib.resources as _ir
            import yaml as _yaml
            gallery_dir = Path(_ir.files("apx_agent").joinpath("coworker_gallery"))  # type: ignore[arg-type]
            matched = [p for p in sorted(gallery_dir.glob("*.yaml")) if p.stem == coworker_spec or _yaml.safe_load(p.read_text()).get("name") == coworker_spec]
            if not matched:
                raise click.ClickException(
                    f"No coworker named {coworker_spec!r} in the gallery. "
                    "Use --coworker list to browse, or --coworker generate to author one."
                )
            gallery_yaml_path = matched[0]

    if scaffold_template == "list" or catalog == "list" or schema == "list":
        ws = _make_ws_for_scaffold(profile)
        if scaffold_template == "list":
            scaffold_template = _pick_template()
        if catalog == "list":
            click.echo("\nAvailable catalogs:")
            catalog = _pick_from_list(_ws_list_catalogs(ws), "Select catalog")
        if schema == "list":
            click.echo(f"\nAvailable schemas in {catalog}:")
            schema = _pick_from_list(_ws_list_schemas(ws, catalog or ""), "Select schema")
        click.echo(f"\n→ apx scaffold {name} --template {scaffold_template or 'coworker'} --catalog {catalog} --schema {schema}\n")
        _scaffold_sanity_check(ws, scaffold_template or "coworker", catalog, schema)
    elif scaffold_template and (catalog or schema):
        # Even without "list", run the sanity check when template+catalog are explicit.
        _scaffold_sanity_check(_make_ws_for_scaffold(profile), scaffold_template, catalog, schema)

    # -----------------------------------------------------------------------
    # Step 1: run the setup wizard or apply CLI defaults.
    # The wizard fires in a TTY (or with --interactive) and asks for anything
    # not already pinned by a flag. --no-interactive / CI stdin skips it.
    # -----------------------------------------------------------------------
    persona: str | None = None
    objective: str | None = None
    interactive_mode = interactive if interactive is not None else (sys.stdin.isatty() and scaffold_template is None)

    join_key: str | None = None
    if interactive_mode:
        _ws = _make_ws_for_scaffold(profile)
        scaffold_target, scaffold_template, catalog, schema, persona, objective, join_key = _scaffold_wizard(
            ws=_ws,
            target=scaffold_target,
            template=scaffold_template,
            catalog=catalog,
            schema=schema,
        )
        # Sanity-check the wizard's output the same way the --catalog list path does.
        _scaffold_sanity_check(_ws, scaffold_template, catalog, schema)
    else:
        scaffold_target = scaffold_target or "apps"
        scaffold_template = scaffold_template or "data"

    instructions: str | None = _prompt_for_instructions() if (interactive_mode and not emit_yaml) else None

    if emit_yaml:
        Path(directory).mkdir(parents=True, exist_ok=True)
        if generated_yaml_str is not None:
            out = Path(directory) / f"{project_name}.yaml"
            out.write_text(generated_yaml_str)
            _echo_scaffold_yaml_done(out, catalog=catalog, schema=schema)
        elif gallery_yaml_path is not None:
            _scaffold_from_gallery(
                gallery_yaml_path=gallery_yaml_path,
                name=project_name,
                directory=Path(directory),
                catalog=catalog,
                schema=schema,
            )
        else:
            _scaffold_to_yaml(
                name=project_name,
                directory=Path(directory),
                scaffold_template=scaffold_template or "base",
                catalog=catalog,
                schema=schema,
                persona=persona,
                join_key=join_key,
                objective=objective,
                instructions=_prompt_for_instructions() if interactive_mode else None,
            )
        return

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
                "# data source: samples.nyctaxi (fallback — couldn't reach the "
                "workspace; run `databricks auth login` if you haven't authenticated, "
                "or pass --catalog/--schema to ground the agent in your own data)"
            )

    if scaffold_target == "apps":
        _scaffold_apps(
            target, project_name, force, catalog, schema, table,
            template=scaffold_template, persona=persona, objective=objective,
            join_key=join_key, lakebase=lakebase, instructions=instructions,
        )
    else:
        _scaffold_model_serving(target, project_name, force, catalog, schema, table)

    click.echo()
    click.echo(f"Scaffolded {name} at {target} (target={scaffold_target}).")
    click.echo(f"Next: cd {name} && uv sync && uv run apx-agent run    # serve locally")
    click.echo("Tip: run `uv run apx-agent doctor` to check your environment before deploying.")
    if scaffold_target == "apps":
        click.echo("      uv run apx-agent deploy                  # → Databricks Apps")
    else:
        click.echo(
            "      uv run apx-agent deploy --model <endpoint> --name <catalog.schema.model>"
        )

    if scaffold_target == "apps" and lakebase:
        _echo_lakebase_guidance()


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
                "`uv run apx-agent run`.",
            )
        ) from e
    finally:
        if added:
            sys.path.remove(cwd)


@agents.command("refresh-schema")
@click.option("--profile", default=None,
              help="Databricks CLI profile to introspect with. "
                   "Falls back to $DATABRICKS_CONFIG_PROFILE.")
@click.option("--prune-missing-tables", is_flag=True, default=False,
              help="DESTRUCTIVE: delete OKF table concepts that the live schema "
                   "no longer lists (drops local-only / hand-authored tables). "
                   "Off by default — a refresh preserves them.")
def refresh_schema(profile: str | None, prune_missing_tables: bool) -> None:
    """Re-introspect this project's catalog.schema and rewrite .apx/schema.json.

    Run inside a scaffolded project. Reads the existing manifest to learn which
    catalog/schema to refresh, re-introspects via the Tables API, and updates the
    manifest so the agent's grounding + landing card reflect the live schema.

    Non-destructive by default: each table's enriched body (Overview/Joins/etc.)
    is preserved and local-only tables not in the live schema are kept. Pass
    ``--prune-missing-tables`` to delete those dropped-table concepts.
    """
    from ._schema import load_baked_schema, APX_DIR, SCHEMA_MANIFEST_NAME
    from ._okf import dump_schema_cache

    existing = load_baked_schema(Path.cwd())
    if not existing or not existing.get("catalog") or not existing.get("schema"):
        raise click.ClickException(
            "No .apx/schema.json found. This command must be run inside a scaffolded project.\n"
            "\n"
            "  To create one:  apx-agent scaffold <project-name>\n"
            "  Then cd into it and run:  apx-agent refresh-schema"
        )
    catalog, schema = existing["catalog"], existing["schema"]
    manifest = _schema_manifest_for_scaffold(catalog, schema, profile=profile)
    if manifest is None:
        raise click.ClickException(
            f"could not read tables for {catalog}.{schema} — check your profile "
            f"and Unity Catalog grants."
        )
    apx = Path.cwd() / APX_DIR
    okf_root = apx / "okf"
    if okf_root.is_dir():
        from datetime import datetime, timezone
        from ._okf import refresh_okf_schema, okf_manifest
        refresh_okf_schema(
            okf_root, manifest,
            timestamp=datetime.now(timezone.utc).isoformat(),
            prune=prune_missing_tables,
        )
        regen = okf_manifest(okf_root)
        if regen is not None:
            (apx / SCHEMA_MANIFEST_NAME).write_text(dump_schema_cache(regen))
        n = len(regen["tables"]) if regen else 0
        click.echo(f"refreshed {okf_root} (+ schema.json cache) — {n} table{'s' if n != 1 else ''} from {catalog}.{schema}")
        return
    out = Path.cwd() / APX_DIR / SCHEMA_MANIFEST_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dump_schema_cache(manifest))
    n = len(manifest["tables"])
    click.echo(f"refreshed {out} — {n} table{'s' if n != 1 else ''} from {catalog}.{schema}")


@agents.command("migrate-to-okf")
@click.option("--force", is_flag=True, help="Overwrite an existing .apx/okf bundle.")
def migrate_to_okf(force: bool) -> None:
    """Convert this project's .apx/schema.json into an .apx/okf/ bundle.

    Reads the existing manifest, emits an OKF v0.1 bundle (the new source of
    truth), then regenerates .apx/schema.json as the derived cache. Idempotent;
    refuses to clobber an existing bundle without --force.
    """
    from datetime import datetime, timezone
    from ._okf import write_okf_bundle, okf_manifest, dump_schema_cache

    apx = Path.cwd() / APX_DIR
    manifest_path = apx / "schema.json"
    okf_root = apx / "okf"
    if not manifest_path.is_file():
        raise click.ClickException(
            "No .apx/schema.json found. Run inside a scaffolded project."
        )
    if okf_root.exists() and not force:
        raise click.ClickException(".apx/okf already exists. Use --force to overwrite.")
    manifest = json.loads(manifest_path.read_text())
    ts = datetime.now(timezone.utc).isoformat()
    write_okf_bundle(manifest, okf_root, timestamp=ts)
    regen = okf_manifest(okf_root)
    if regen is not None:
        manifest_path.write_text(dump_schema_cache(regen))
    click.echo(f"Wrote OKF bundle to {okf_root} (schema.json regenerated as derived cache).")


@agents.command("pull-comments")
@click.option("--profile", default=None,
              help="Databricks CLI profile to read UC comments with. "
                   "Falls back to $DATABRICKS_CONFIG_PROFILE.")
@click.option("--overwrite", is_flag=True,
              help="Replace already-curated OKF descriptions with UC comments "
                   "(default: only fill empty cells).")
def pull_comments(profile: str | None, overwrite: bool) -> None:
    """Enrich this project's OKF bundle from Unity Catalog COMMENTs (read-only).

    Reads table/column comments from UC via the Tables API and fills empty
    # Schema Description cells + a # Overview per table. Non-destructive: curated
    descriptions are kept unless --overwrite. Does NOT write to Unity Catalog.
    """
    from ._okf import apply_uc_comments
    from ._schema import load_baked_schema, APX_DIR

    existing = load_baked_schema(Path.cwd())
    if not existing or not existing.get("catalog") or not existing.get("schema"):
        raise click.ClickException(
            "No .apx grounding found. Run inside a scaffolded project with an OKF bundle."
        )
    okf_root = Path.cwd() / APX_DIR / "okf"
    if not okf_root.is_dir():
        raise click.ClickException(
            "No .apx/okf bundle found. Run `apx-agent agents migrate-to-okf` first."
        )
    catalog, schema = existing["catalog"], existing["schema"]
    ws = _make_ws_for_scaffold(profile)
    if ws is None:
        raise click.ClickException(
            f"Could not connect to a workspace to read comments for {catalog}.{schema}."
        )
    comments: dict = {}
    try:
        for t in ws.tables.list(catalog_name=catalog, schema_name=schema):
            name = getattr(t, "name", None)
            if not name:
                continue
            cmap: dict = {"_table": getattr(t, "comment", None) or ""}
            for c in (getattr(t, "columns", None) or []):
                if getattr(c, "name", None):
                    cmap[c.name] = getattr(c, "comment", None) or ""
            comments[name] = cmap
    except Exception as e:
        raise click.ClickException(f"Could not read comments for {catalog}.{schema}: {e}")
    n = apply_uc_comments(okf_root, comments, overwrite=overwrite)
    click.echo(
        f"pull-comments: enriched {n} table{'s' if n != 1 else ''} in {okf_root} "
        f"from {catalog}.{schema} (read-only)."
    )


def _port_is_free(port: int, host: str) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _identify_port_occupant(port: int, host: str) -> dict:
    """Return info about the process on *port*.

    Keys: ``pid`` (int|None), ``name`` (str), ``is_apx`` (bool).
    Tries ``lsof`` + ``ps`` to get the PID/command, then a quick HTTP hit
    to ``/info`` or ``/health`` to read the agent name when it's an apx server.
    """
    import subprocess, urllib.request, json

    pid: int | None = None
    cmd = ""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=3
        )
        pids = [p.strip() for p in result.stdout.strip().splitlines() if p.strip()]
        if pids:
            pid = int(pids[0])
            ps = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=2,
            )
            cmd = ps.stdout.strip()
    except Exception:
        pass

    is_apx = any(kw in cmd for kw in ("apx-agent", "apx_agent", "uvicorn", "start_server"))

    # Try reading the agent name from a live /info endpoint.
    agent_name: str | None = None
    for path in ("/info", "/health"):
        try:
            with urllib.request.urlopen(
                f"http://{host}:{port}{path}", timeout=1
            ) as resp:
                data = json.loads(resp.read())
                agent_name = (
                    data.get("name")
                    or data.get("agent_name")
                    or data.get("agent", {}).get("name")
                )
                if agent_name:
                    is_apx = True
                    break
        except Exception:
            pass

    if not agent_name and pid:
        # Try the working directory of the process (most reliable: it's the
        # project folder, so basename == agent name even when /info is down).
        try:
            cwd_result = subprocess.run(
                ["lsof", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=2,
            )
            for line in cwd_result.stdout.splitlines():
                if line.startswith("n") and line != "n/":
                    cwd_name = Path(line[1:]).name
                    if cwd_name and cwd_name not in (".", "/"):
                        agent_name = cwd_name
                        is_apx = True
                        break
        except Exception:
            pass

    if not agent_name and cmd:
        # Last resort: pull --app-dir value from the uvicorn command line.
        parts = cmd.split()
        for i, part in enumerate(parts):
            if part == "--app-dir" and i + 1 < len(parts):
                agent_name = Path(parts[i + 1]).name
                break

    display_name = agent_name or "agent"
    return {"pid": pid, "name": display_name, "cmd": cmd, "is_apx": is_apx}


def _find_free_port(preferred: int, *, host: str = "127.0.0.1", max_tries: int = 10) -> int:
    """Return a port to listen on, handling conflicts interactively when in a TTY.

    - Port is free: return it immediately.
    - Port is busy + TTY detected as apx-agent: offer kill-and-reclaim or next port.
    - Port is busy + TTY non-apx: offer next port or kill anyway.
    - Port is busy + non-TTY: auto-advance silently.
    """
    import signal, time

    if _port_is_free(preferred, host):
        return preferred

    # Port is in use.
    if sys.stdin.isatty():
        occupant = _identify_port_occupant(preferred, host)
        name = occupant["name"]
        pid = occupant["pid"]

        if occupant["is_apx"] and pid:
            click.echo(f"\nPort {preferred} is running: {name} (PID {pid})")
        elif pid:
            click.echo(f"\nPort {preferred} is in use by: {name} (PID {pid})")
        else:
            click.echo(f"\nPort {preferred} is already in use.")

        next_port = next(
            (p for p in range(preferred + 1, preferred + max_tries) if _port_is_free(p, host)),
            preferred + 1,
        )
        options = [f"Use port {next_port} instead"]
        if pid:
            options.append(f"Kill {name} (PID {pid}) and use port {preferred}")
        click.echo()
        for i, opt in enumerate(options, 1):
            click.echo(f"  {i}. {opt}")
        choice = click.prompt("\nChoose", type=click.IntRange(1, len(options)), default=1)

        if choice == 1:
            if next_port != preferred + 1:
                click.echo(f"Using port {next_port}.", err=True)
            return next_port
        else:
            # Kill the occupant and reclaim the preferred port.
            try:
                os.kill(pid, signal.SIGTERM)
                click.echo(f"  Sent SIGTERM to {name} (PID {pid})…", err=True)
                for _ in range(20):
                    time.sleep(0.25)
                    if _port_is_free(preferred, host):
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            click.echo(f"  Port {preferred} is free.", err=True)
            return preferred

    # Non-interactive: silently advance.
    for candidate in range(preferred + 1, preferred + max_tries):
        if _port_is_free(candidate, host):
            click.echo(
                f"Port {preferred} is already in use — using {candidate} instead.",
                err=True,
            )
            return candidate
    return preferred


def _find_runnable_agents(cwd: Path) -> list[tuple[str, Path]]:
    """Return (name, project_dir) for every runnable agent project under cwd.

    Searches the cwd itself, then up to two levels of subdirectories (so both
    ./my-agent/ and ./python/my-agent/ are found). A directory is "runnable"
    when it contains the apps layout (agent_server/start_server.py) or the
    model-serving layout (app.py without databricks.yml).
    """

    def _is_runnable(d: Path) -> bool:
        return (
            (d / "agent_server" / "start_server.py").exists()
            or ((d / "app.py").exists() and not (d / "databricks.yml").exists())
        )

    _SKIP = {"__pycache__", ".venv", "node_modules", ".git"}

    results: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    def _scan(d: Path, depth: int) -> None:
        if d in seen:
            return
        seen.add(d)
        if _is_runnable(d):
            results.append((d.name, d))
            return  # don't recurse into a runnable project
        if depth > 0:
            try:
                children = sorted(d.iterdir())
            except PermissionError:
                return
            for child in children:
                if child.is_dir() and child.name not in _SKIP and not child.name.startswith("."):
                    _scan(child, depth - 1)

    _scan(cwd, depth=2)
    return results


@agents.command()
@click.argument("spec", default=None, required=False, metavar="SPEC")
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
def run(spec: str | None, module: str | None, port: int, host: str, reload: bool) -> None:
    """Run the agent locally via uvicorn against the FastAPI app.

    SPEC can be a project directory, a .yaml spec name (stem or full path), or
    'list' to pick interactively from all available agents in this directory.
    Omit SPEC to auto-detect the project in the current directory.
    """
    try:
        import uvicorn
    except ImportError as e:
        raise click.ClickException(
            "uvicorn is required for `apx-agent run`. Install with: "
            "uv add 'apx-agent[apps]'  (or: uv add 'uvicorn[standard]')"
        ) from e

    # --- resolve SPEC to a project directory ---
    from ._doctor import _is_apx_project

    cwd = Path.cwd()
    spec_is_yaml = bool(spec) and spec.endswith((".yaml", ".yml"))

    if spec == "list":
        agents = _find_runnable_agents(cwd)
        if not agents:
            raise click.ClickException(
                "No runnable agent projects found in this directory.\n"
                "Run 'apx-agent scaffold <name>' to create one."
            )
        click.echo("Available agents:")
        for i, (name, path) in enumerate(agents, 1):
            rel = path.relative_to(cwd) if path != cwd else Path(".")
            click.echo(f"  {i:2}. {name}  ({rel})")
        idx = click.prompt("Choose", type=click.IntRange(1, len(agents)), default=1)
        _, project_dir = agents[idx - 1]
        os.chdir(project_dir)
    elif spec_is_yaml:
        # A .yaml spec has no project files — generate one into a temp dir and
        # serve from there, mirroring `deploy <spec>.yaml`. The temp dir lives
        # for the server's lifetime and is cleaned up on process exit. Edit the
        # YAML and re-run to pick up changes (the generated project is derived,
        # not the source of truth — that's the YAML).
        import atexit
        import shutil
        import tempfile
        from ._project_gen import generate_project
        from ._yaml_spec import SpecValidationError, load_spec

        assert spec is not None  # spec_is_yaml implies spec is truthy
        yaml_path = Path(spec)
        if not yaml_path.exists():
            raise click.ClickException(f"Spec file not found: {spec}")
        try:
            config = load_spec(yaml_path)
        except SpecValidationError as e:
            raise click.ClickException(f"Invalid spec {yaml_path}: {e}") from e
        tmp = tempfile.mkdtemp(prefix="apx_run_")
        atexit.register(lambda: shutil.rmtree(tmp, ignore_errors=True))
        project_dir = Path(tmp) / config.name
        generate_project(config, project_dir, source_dir=yaml_path.parent)
        # Ground the served agent: bake .apx/schema.json so the DataAgent
        # declares its UC tables (no runtime DESCRIBE; dev-UI DATA panel shows
        # them). Best-effort — falls back to live introspection if it can't read.
        if _bake_schema_into_project(project_dir, config, None):
            click.echo("# baked .apx/schema.json from the UC schema", err=True)
        click.echo(
            f"# Generated a local project from {yaml_path.name} "
            "(edit the YAML and re-run to update)",
            err=True,
        )
        os.chdir(project_dir)
        # Pin config resolution to the generated project. The apps runtime's
        # _load_agent_config() otherwise walks up from __main__ (uvicorn / the
        # apx-agent bin) and can find a nearer [tool.apx.agent] than this one.
        os.environ["APX_PYPROJECT"] = str(project_dir / "pyproject.toml")
    elif spec is not None:
        # A directory name (or bare stem) of an already-materialized project.
        spec_path = Path(spec)
        candidate_dir: Path | None = None
        if spec_path.is_dir():
            candidate_dir = spec_path.resolve()
        else:
            # Match against real apx projects only — _detect_target defaults to
            # "apps" for any directory, so checking it would falsely match cwd.
            for d in [cwd / spec, cwd]:
                if d.is_dir() and _is_apx_project(d):
                    candidate_dir = d
                    break
        if candidate_dir is None:
            hint = (
                f"{spec!r} looks like a spec file but doesn't exist."
                if spec.endswith((".yaml", ".yml"))
                else f"No runnable agent project found for {spec!r}."
            )
            raise click.ClickException(
                f"{hint}\nTry 'apx-agent run list' to see what's available, or "
                "'apx-agent run <spec>.yaml' to run from a spec."
            )
        if candidate_dir != cwd:
            os.chdir(candidate_dir)

    if module is None:
        detected = _detect_target()
        module = _RUN_MODULE_BY_TARGET[detected.target]
        click.echo(
            f"target: {detected.target} (auto-detected: {detected.reason}) "
            f"→ serving {module}",
            err=True,
        )
        if detected.target == "apps":
            model = os.environ.get("APX_MODEL", _APPS_DEFAULT_MODEL)
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
    port = _find_free_port(port, host=host)
    uvicorn.run(module, host=host, port=port, reload=reload, app_dir=str(Path.cwd()))


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def _scan_apx_ports(
    host: str = "127.0.0.1",
    start: int = 8000,
    count: int = 20,
) -> list[dict]:
    """Return occupant info for every port in [start, start+count) that is in use."""
    occupied = []
    for port in range(start, start + count):
        if not _port_is_free(port, host):
            info = _identify_port_occupant(port, host)
            info["port"] = port
            occupied.append(info)
    return occupied


def _kill_pid(pid: int, name: str) -> bool:
    """SIGTERM → wait up to 5 s → SIGKILL. Returns True when port eventually freed."""
    import signal, time

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    for _ in range(20):
        time.sleep(0.25)
        try:
            os.kill(pid, 0)  # still alive?
        except ProcessLookupError:
            return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


@agents.command()
@click.argument("agent_name", default=None, required=False, metavar="AGENT")
@click.option("--port", default=None, type=int, help="Target a specific port instead of searching.")
@click.option("--host", default="127.0.0.1", help="Host. Default: 127.0.0.1.")
@click.option("--all", "stop_all", is_flag=True, help="Stop every apx-agent process found.")
def stop(agent_name: str | None, port: int | None, host: str, stop_all: bool) -> None:
    """Stop a running apx-agent local server.

    AGENT is the project name (e.g. 'hello-world'). When omitted, stops
    whatever is running on --port (default: searches 8000-8019).

    Examples:

    \b
      apx-agent stop                  # stop whatever is on port 8000
      apx-agent stop hello-world      # find and stop hello-world
      apx-agent stop --port 8001      # stop whatever is on 8001
      apx-agent stop --all            # stop every apx-agent found
    """
    if port is not None:
        # Explicit port: just kill whatever is there.
        if _port_is_free(port, host):
            raise click.ClickException(f"Nothing is running on port {port}.")
        info = _identify_port_occupant(port, host)
        info["port"] = port
        candidates = [info]
    else:
        candidates = _scan_apx_ports(host)
        if not candidates:
            raise click.ClickException("No apx-agent processes found on ports 8000-8019.")

    # Filter by name when given.
    if agent_name:
        slug = agent_name.lower().replace("-", "_")
        matched = [
            c for c in candidates
            if slug in (c.get("name") or "").lower().replace("-", "_")
            or slug in (c.get("cmd") or "").lower()
        ]
        if not matched:
            running = ", ".join(
                f"{c.get('name','?')} :{c['port']}" for c in candidates
            ) or "none"
            raise click.ClickException(
                f"No running agent matching '{agent_name}'. Running: {running}"
            )
        candidates = matched

    if not stop_all and len(candidates) > 1:
        click.echo("Multiple agents running — pick one (or use --all):\n")
        for i, c in enumerate(candidates, 1):
            click.echo(f"  {i:2}. {c.get('name','?'):<28} :{c['port']}")
        idx = click.prompt("\nChoose", type=click.IntRange(1, len(candidates)), default=1)
        candidates = [candidates[idx - 1]]

    for c in candidates:
        pid = c.get("pid")
        name = c.get("name") or "agent"
        p = c["port"]
        if pid:
            click.echo(f"Stopping {name} (PID {pid}, port {p})…")
            _kill_pid(pid, name)
            click.echo(f"  Stopped.")
        else:
            click.echo(
                f"Found process on port {p} but could not determine PID.\n"
                f"Run: lsof -ti :{p} | xargs kill"
            )


# ---------------------------------------------------------------------------
# apps
# ---------------------------------------------------------------------------


@agents.command("apps")
@click.option("--profile", default=None, help="Databricks CLI profile to use.")
@click.option("--host", default=None, help="Workspace URL (overrides profile).")
def apps_list(profile: str | None, host: str | None) -> None:
    """List Databricks Apps deployed in the workspace.

    Shows name, state, URL, and last-updated time for every app visible
    to the current credentials. Use --profile to pick a specific workspace.

    Examples:

    \b
      apx-agent apps                       # list apps in default workspace
      apx-agent apps --profile fe-stable   # list apps in fe-stable workspace
    """
    profile = profile or _read_apx_agent_config().get("profile") or os.environ.get("DATABRICKS_CONFIG_PROFILE")

    # _connect_workspace handles auth errors interactively; host override goes
    # via a separate Config when supplied.
    if host:
        try:
            from databricks.sdk import WorkspaceClient
            from databricks.sdk.config import Config
            cfg = Config(profile=profile, host=host)
            ws = WorkspaceClient(config=cfg)
        except Exception as exc:
            raise click.ClickException(f"Could not connect to {host}: {exc}")
    else:
        ws, cfg = _connect_workspace(profile)

    try:
        app_list = list(ws.apps.list())
    except Exception as exc:
        raise click.ClickException(f"Failed to list apps: {exc}")

    if not app_list:
        click.echo("No Databricks Apps found in this workspace.")
        click.echo("Deploy one with:  apx-agent deploy")
        return

    # Resolve workspace host for URL construction.
    ws_host = cfg.host.rstrip("/") if cfg.host else ""

    # Header
    click.echo(f"\n{'NAME':<32} {'STATE':<12} {'UPDATED':<20}  URL")
    click.echo("─" * 100)

    for app in sorted(app_list, key=lambda a: (a.name or "")):
        name = app.name or "?"
        state = (
            (app.app_status.state.value if hasattr(app.app_status.state, "value") else str(app.app_status.state))
            if app.app_status and app.app_status.state
            else "UNKNOWN"
        )
        # Colour-code state for quick scanning.
        state_display = {
            "RUNNING": click.style("RUNNING", fg="green"),
            "STARTING": click.style("STARTING", fg="yellow"),
            "DEPLOYING": click.style("DEPLOYING", fg="yellow"),
            "ERROR": click.style("ERROR", fg="red"),
            "STOPPED": click.style("STOPPED", fg="red"),
            "IDLE": click.style("IDLE", fg="blue"),
        }.get(state, state)

        updated = ""
        if app.update_time:
            try:
                from datetime import datetime, timezone
                ts = app.update_time
                if isinstance(ts, (int, float)):
                    ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                updated = ts.strftime("%Y-%m-%d %H:%M") if isinstance(ts, datetime) else str(ts)[:16]
            except Exception:
                pass

        url = getattr(app, "url", None) or ""
        if not url and ws_host and name:
            url = f"{ws_host}/apps/{name}"

        click.echo(f"{name:<32} {state_display:<12} {updated:<20}  {url}")

    click.echo()


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------


def _warn_empty_predictions(result: Any) -> None:
    """Print a warning when some eval predictions returned no output.

    MLflow's parallel eval harness can drop predictions to None on transient
    FMAPI/warehouse failures. Without this, the mean score alone misreads a
    concurrency artifact as a low-quality agent.
    """
    from apx_agent._eval import _count_empty_predictions

    empty = _count_empty_predictions(result)
    if empty is None or empty == 0:
        return
    total = len(result.result_df)
    click.echo(
        f"⚠ {empty}/{total} predictions returned no output "
        "(possible transient failures — throttle with "
        "MLFLOW_GENAI_EVAL_MAX_WORKERS)."
    )


@eval_group.command("run")
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
@click.option(
    "--judge-model", default=None,
    help="LLM judge override for the default scorers. The default judge needs "
         "OPENAI_API_KEY; pass 'databricks' or a '<provider>:/<model>' URI (e.g. "
         "'databricks:/databricks-claude-sonnet-4-6') to use a Databricks/Anthropic "
         "judge instead. Only used for in-process eval.",
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
    judge_model: str | None,
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
        _warn_empty_predictions(result)
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
        judge_model=judge_model,
        user_token=user_token,
        experiment=effective_experiment,
    )
    click.echo(f"Eval complete. Result: {result}")
    _warn_empty_predictions(result)


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------


def _inject_framework_source(project_dir: Path, framework_python: Path) -> None:
    """Append [tool.uv.sources] to the generated pyproject.toml pointing at the
    framework source so _ensure_apx_wheel can build and bundle the wheel.

    Without this, deploying from source produces an App container that tries to
    install apx-agent from a registry where it may not be published yet.
    """
    pyproject = project_dir / "pyproject.toml"
    text = pyproject.read_text()
    if "[tool.uv.sources]" in text:
        return
    abs_src = framework_python.resolve()
    text += f'\n[tool.uv.sources]\napx-agent = {{ path = "{abs_src}", editable = true }}\n'
    pyproject.write_text(text)


def _deploy_from_yaml(
    yaml_path: Path,
    profile: str | None,
    bundle_target: str,
    no_run: bool,
    readyz_gate: bool,
    json_output: bool,
    assume_yes: bool,
) -> None:
    """Read *yaml_path*, generate a temp project, and deploy it via _deploy_apps."""
    import tempfile
    from ._yaml_spec import load_spec, SpecValidationError
    from ._project_gen import generate_project

    try:
        config = load_spec(yaml_path)
    except SpecValidationError as e:
        raise click.ClickException(f"Invalid spec {yaml_path}: {e}") from e

    with tempfile.TemporaryDirectory(prefix="apx_deploy_") as tmp:
        project_dir = Path(tmp) / config.name
        generate_project(config, project_dir, source_dir=yaml_path.parent)
        # Ground the deployed agent: bake .apx/schema.json so the DataAgent
        # declares its UC tables (no runtime DESCRIBE; dev-UI DATA panel shows
        # them). The bundle step copies .apx/ into .build/. Best-effort.
        if _bake_schema_into_project(project_dir, config, profile):
            click.echo("  baked .apx/schema.json from the UC schema", err=True)

        # When running inside the framework source repo, inject the editable
        # source so _ensure_apx_wheel can build and bundle the wheel.
        framework_python = _find_nearby_framework_python(Path.cwd())
        if framework_python is not None:
            _inject_framework_source(project_dir, framework_python)

        # When running inside the framework source repo, inject the editable
        # source so _ensure_apx_wheel can build and bundle the wheel.
        framework_python = _find_nearby_framework_python(Path.cwd())
        if framework_python is not None:
            _inject_framework_source(project_dir, framework_python)

        import os
        orig = os.getcwd()
        try:
            os.chdir(project_dir)
            _deploy_apps(
                module="agent:agent",
                profile=profile,
                bundle_target=bundle_target,
                no_run=no_run,
                auto_update_yml=False,  # generated yml is complete; agent.py is codegen'd
                auto_build_wheel=True,
                auto_experiment=True,
                vars=(),
                json_output=json_output,
                readyz_gate=readyz_gate,
            )
        finally:
            os.chdir(orig)


# deploy options only the model-serving branch consumes (issue #413):
# param name → flag spelling, echoed in the "ignored with --target apps"
# warning when the caller passed one explicitly. --experiment is handled
# separately with a pointer at the apps-flow equivalent.
_MODEL_SERVING_ONLY_DEPLOY_FLAGS = (
    ("publish_tools", "--publish-tools/--no-publish-tools"),
    ("set_uc_tags", "--set-uc-tags/--no-set-uc-tags"),
    ("agent_name", "--agent-name"),
    ("no_deploy", "--no-deploy"),
    ("capture_env_vars", "--capture-env-vars/--no-capture-env-vars"),
    ("allow_env_vars", "--allow-env-var"),
    ("scale_to_zero", "--scale-to-zero/--no-scale-to-zero"),
    ("workload_size", "--workload-size"),
    ("env_suffix", "--env-suffix"),
    ("version", "--version"),
)

# deploy options only the apps branch consumes (issue #415): the mirror of
# _MODEL_SERVING_ONLY_DEPLOY_FLAGS above — echoed in the "ignored with
# --target model-serving" warning when the caller passed one explicitly.
_APPS_ONLY_DEPLOY_FLAGS = (
    ("env_pairs", "--env"),
    ("secret_env_pairs", "--secret-env"),
    ("readyz_retries", "--readyz-retries"),
)


@agents.command()
@click.argument("spec", required=False, default=None, metavar="[SPEC.yaml]")
@click.option("--module", default=None, help='Agent module spec. Default '
              '"agent:agent" for both --target model-serving and '
              '--target apps (ADK-style top-level layout).')
@click.option(
    "--target", "target", default=None,
    type=click.Choice(["model-serving", "apps"]),
    help="Deployment target. Auto-detected from the project layout when "
         "omitted: apps if agent_server/start_server.py exists; model-serving "
         "if app.py exists without databricks.yml; apps otherwise (the "
         "catch-all default). 'model-serving' runs the canonical log_agent + "
         "databricks.agents.deploy flow. 'apps' runs the Databricks Asset "
         "Bundle deploy + run flow. The two targets accept different option "
         "sets — see --help for details.",
)
@click.option("--model", default=None, help="Databricks serving endpoint for "
              "the LLM. Required when --target model-serving.")
@click.option(
    "--name", "registered_model_name", default=None,
    help="UC three-part name to register the model under (catalog.schema.model). "
         "Falls back to [tool.apx.agent].registered_model in pyproject.toml; "
         "required when --target model-serving and neither is set.",
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
    help="Post-deploy health gate, ON by default for BOTH targets. With "
         "--target apps: after the app reaches RUNNING, GET <app_url>/readyz "
         "and FAIL the deploy if the agent doesn't report ready (answer + "
         "trace). With --target model-serving: after databricks.agents.deploy, "
         "poll the serving endpoint to READY and send one smoke invocation, "
         "FAILING the deploy if the endpoint never answers. A green deploy "
         "then means the agent works, not just that the container booted / "
         "the endpoint update was accepted.",
)
@click.option(
    "--register-uc/--no-register-uc", default=True,
    help="After the app is live, register a UC model-version *manifest* for it "
         "(log_agent + apx.* tags, tagged apx.serving=apps) so the Apps agent "
         "gets a version ledger and shows up in apx-agent list / topology / "
         "watchdog. NOT promoted to serving — the App serves traffic, the UC "
         "version is a record of what it's running. Skips with a notice if no "
         "UC name / model is configured. ON by default. Only used by "
         "--target apps.",
)
@click.option(
    "--uc-name", default=None,
    help="UC three-part name (catalog.schema.model) to register the Apps "
         "version manifest under. Overrides [tool.apx.agent].registered_model "
         "and the catalog/schema-composed default. Only used by --target apps.",
)
@click.option(
    "--var", "vars", multiple=True,
    help="Extra `--var key=value` pairs to forward to `databricks bundle "
         "deploy + bundle run`. Repeatable. Use to override resources, "
         "wire in a vector_search_index, etc.",
)
@click.option(
    "--env", "env_pairs", multiple=True, metavar="KEY=VALUE",
    help="Runtime env var for the deployed app. Repeatable. Merged into "
         "resources.apps.<app>.config.env in databricks.yml BEFORE the "
         "bundle deploy so the value reaches the running app. The merge "
         "PERSISTS in databricks.yml — commit it to keep it, revert the "
         "file to drop it. An entry whose name already exists in "
         "config.env is NEVER clobbered: it is skipped with a warning "
         "(edit databricks.yml to change it). Only used by --target apps.",
)
@click.option(
    "--secret-env", "secret_env_pairs", multiple=True, metavar="KEY=SCOPE/KEY",
    help="Secret-backed runtime env var: KEY=scope/key names a Databricks "
         "secret REFERENCE, never the secret value. Declares a secret app "
         "resource (permission READ) plus an env entry pointing at it via "
         "valueFrom, so the app resolves the value at runtime — the literal "
         "secret never appears in databricks.yml or the deploy output. Same "
         "persistence and never-clobber semantics as --env. Only used by "
         "--target apps.",
)
@click.option(
    "--json-output", is_flag=True, default=False,
    help="Emit a single JSON object on stdout summarising the run. "
         "Progress logs are routed to stderr.",
)
@click.option("--no-deploy", is_flag=True,
              help="Log + register only, skip databricks.agents.deploy. "
                   "Only used by --target model-serving.")
@click.option(
    "--experiment", default=None,
    help="MLflow experiment name/path. Falls back to [tool.apx.agent].experiment "
         "in pyproject.toml; falls back to MLflow's default when neither is set. "
         "Only used by --target model-serving.",
)
@click.option(
    "--publish-tools/--no-publish-tools", default=True,
    help="Publish @tool(uc=...) decorated tools to Unity Catalog before logging. "
         "On by default; pass --no-publish-tools to skip. "
         "Only used by --target model-serving.",
)
@click.option(
    "--set-uc-tags/--no-set-uc-tags", default=True,
    help="Write apx.agent.* UC tags on the registered model after deploy so "
         "the agent shows up in apx-agent list / topology / watchdog crawls. "
         "On by default. Only used by --target model-serving.",
)
@click.option(
    "--agent-name", default=None,
    help="Friendly agent name used for the apx.agent.name UC tag. Falls back to "
         "[tool.apx.agent].name in pyproject.toml; falls back to the short part "
         "of --name. Only used by --target model-serving.",
)
@click.option(
    "--capture-env-vars/--no-capture-env-vars", default=False,
    help="Allow MLflow to record env vars referenced in the agent's dependency "
         "chain into the logged model artifact. OFF by default to prevent "
         "developer-shell secrets (ATLASSIAN_API_KEY, GEMINI_API_KEY, etc.) "
         "from being baked into the deployed image. Sets "
         "MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING=false for the duration of "
         "log_agent and restores the caller's environment afterward. "
         "Only used by --target model-serving.",
)
@click.option(
    "--allow-env-var", "allow_env_vars", multiple=True, metavar="NAME",
    help="Explicitly allow a specific env var to be recorded. Repeatable: "
         "--allow-env-var DATABRICKS_HOST --allow-env-var DATABRICKS_TOKEN. "
         "Note: MLflow's MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING is all-or-nothing; "
         "passing any --allow-env-var flips capture back on for everything and "
         "prints a warning listing what you asked to allow vs the full capture "
         "set you'll actually get. Only used by --target model-serving.",
)
@click.option(
    "--scale-to-zero/--no-scale-to-zero", "scale_to_zero", default=None,
    help="Scale the serving endpoint to zero when idle. When neither flag is "
         "passed, databricks.agents.deploy's own default applies — the flag "
         "is only forwarded when set explicitly. "
         "Only used by --target model-serving.",
)
@click.option(
    "--workload-size", default=None,
    type=click.Choice(["Small", "Medium", "Large"]),
    help="Serving endpoint workload size. When omitted, "
         "databricks.agents.deploy's own default applies — the value is only "
         "forwarded when set explicitly. Only used by --target model-serving.",
)
@click.option(
    "--env-suffix", default=None, metavar="SUFFIX",
    help="Append '-SUFFIX' to the UC model short name, so --env-suffix "
         "staging registers catalog.schema.model-staging (and the serving "
         "endpoint name derived from it follows). This is naming-convention "
         "support for dev/staging/prod, NOT per-environment config "
         "management. Only used by --target model-serving.",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Print the deploy plan and exit 0 WITHOUT changing anything: no "
         "bundle calls, no databricks.yml writes (--env is reported, not "
         "merged), no MLflow logging, no uv.lock rewrite, no network. Works "
         "with both targets; combine with --json-output for a machine-"
         "readable plan object ({\"plan\": true, ...}).",
)
@click.option(
    "--timeout", "timeout_seconds", default=300, show_default=True, type=int,
    metavar="SECONDS",
    help="Post-deploy readiness budget in seconds. Drives the `databricks "
         "apps get` ACTIVE/RUNNING poll (--target apps) and the serving-"
         "endpoint READY poll inside the health gate (--target "
         "model-serving).",
)
@click.option(
    "--readyz-retries", "readyz_retries", default=5, show_default=True,
    type=int, metavar="N",
    help="Attempts for the /readyz gate GET against the deployed app before "
         "it is declared unreachable. Only used by --target apps.",
)
@click.option(
    "--version", "version", default=None, type=int, metavar="N",
    help="Deploy an ALREADY-LOGGED model version N instead of logging a new "
         "one: skips publish_tools + log_agent (ledger: skipped) and goes "
         "straight to databricks.agents.deploy(name, N) + health gate + UC "
         "tags. Incompatible with --no-deploy. Only used by "
         "--target model-serving.",
)
@click.option(
    "--yes", "-y", "assume_yes", is_flag=True,
    help="Skip the secret-scan confirmation prompt. Use in CI / scripts.",
)
def deploy(
    spec: str | None,
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
    register_uc: bool,
    uc_name: str | None,
    vars: tuple[str, ...],
    env_pairs: tuple[str, ...],
    secret_env_pairs: tuple[str, ...],
    json_output: bool,
    no_deploy: bool,
    experiment: str | None,
    publish_tools: bool,
    set_uc_tags: bool,
    agent_name: str | None,
    capture_env_vars: bool,
    allow_env_vars: tuple[str, ...],
    scale_to_zero: bool | None,
    workload_size: str | None,
    env_suffix: str | None,
    dry_run: bool,
    timeout_seconds: int,
    readyz_retries: int,
    version: int | None,
    assume_yes: bool,
) -> None:
    """Log the agent to MLflow + deploy + UC-tag in one command.

    Pass ``--dry-run`` to print the resolved plan (target, names, experiment,
    step list) and exit without changing anything.

    With ``--target model-serving`` (default) runs the canonical flow:

      1. publish_tools_to_uc(agent)    — register any @tool(uc=...) tools
      2. log_agent(agent, ...)         — log to MLflow + register in UC
      3. databricks.agents.deploy(...) — promote to a serving endpoint
      4. health gate                   — poll the endpoint to READY + one
         smoke invocation; FAIL the deploy if the agent doesn't answer
      5. set_uc_tags_for_agent(...)    — write apx.agent.* tags

    Toggle individual stages with --no-publish-tools, --no-deploy,
    --no-readyz-gate, or --no-set-uc-tags.

    With ``--target apps`` runs the Databricks Asset Bundle deploy flow:

      1. databricks bundle validate    — sanity check the bundle config
      2. databricks bundle deploy      — push the app + sync resources
      3. databricks bundle run <app>   — start the app (skipped with --no-run)
      4. databricks apps get           — poll until ACTIVE/RUNNING + readyz
         gate (both skipped with --no-run: the app was never started)
      5. register_apps_manifest(...)   — register a UC version manifest + tags
         (skipped with --no-register-uc, or when no UC name / model resolves)

    The Apps version manifest (step 5) registers a UC model version tagged
    ``apx.serving=apps`` so the Apps agent gets a version ledger and shows up in
    ``apx agents list`` / topology / watchdog — but it is NOT promoted to a
    serving endpoint. The App serves traffic; the UC version records what it's
    running. It runs only when a UC name and model are configured (see
    ``docs/engine-scope/apps-uc-registry-shim-design.md``).
    """
    if spec and (spec.endswith(".yaml") or spec.endswith(".yml")):
        if dry_run:
            raise click.UsageError(
                "--dry-run is not supported with a SPEC.yaml deploy — "
                "scaffold the project first, then run "
                "`apx-agent agents deploy --dry-run` from it."
            )
        _deploy_from_yaml(
            yaml_path=Path(spec),
            profile=profile,
            bundle_target=bundle_target,
            no_run=no_run,
            readyz_gate=readyz_gate,
            json_output=json_output,
            assume_yes=assume_yes,
        )
        return

    if target is None:
        detected = _detect_target()
        target = detected.target
        target_reason = f"auto-detected: {detected.reason}"
        click.echo(
            f"target: {detected.target} (auto-detected: {detected.reason}; "
            "override with --target)",
            err=True,
        )
    else:
        target_reason = "explicit --target"

    if target == "apps":
        # Flag hygiene (issue #413): the apps branch never reads the
        # model-serving-only options — warn instead of silently dropping them.
        # Warn (not error) so one script can be shared across targets.
        if experiment is not None:
            click.echo(
                "# WARNING: --experiment is ignored with --target apps — it is "
                "only used by --target model-serving. For apps, pass "
                "--var mlflow_experiment_id=<id> or rely on --auto-experiment "
                "(on by default).",
                err=True,
            )
        ctx = click.get_current_context()
        ignored = [
            flag for param, flag in _MODEL_SERVING_ONLY_DEPLOY_FLAGS
            if ctx.get_parameter_source(param)
            == click.core.ParameterSource.COMMANDLINE
        ]
        if ignored:
            click.echo(
                "# WARNING: ignored with --target apps (only used by "
                f"--target model-serving): {', '.join(ignored)}",
                err=True,
            )
        if dry_run:
            _emit_apps_deploy_plan(
                module=module or "agent:agent",
                target_reason=target_reason,
                bundle_target=bundle_target,
                no_run=no_run,
                auto_build_wheel=auto_build_wheel,
                auto_experiment=auto_experiment,
                readyz_gate=readyz_gate,
                register_uc=register_uc,
                uc_name=uc_name,
                vars=vars,
                env_pairs=env_pairs,
                secret_env_pairs=secret_env_pairs,
                json_output=json_output,
                timeout_seconds=timeout_seconds,
                readyz_retries=readyz_retries,
            )
            return
        _deploy_apps(
            module=module or "agent:agent",
            profile=profile,
            bundle_target=bundle_target,
            no_run=no_run,
            auto_update_yml=auto_update_yml,
            auto_build_wheel=auto_build_wheel,
            auto_experiment=auto_experiment,
            vars=vars,
            env_pairs=env_pairs,
            secret_env_pairs=secret_env_pairs,
            json_output=json_output,
            readyz_gate=readyz_gate,
            register_uc=register_uc,
            uc_name=uc_name,
            poll_timeout_seconds=timeout_seconds,
            readyz_attempts=readyz_retries,
        )
        return

    # --- model-serving target (the legacy path) ---
    # Flag hygiene (issue #415, mirroring #413): the model-serving branch
    # never reads the apps-only runtime-env options — warn instead of
    # silently dropping them. Warn (not error) so one script can be shared
    # across targets.
    ms_ctx = click.get_current_context()
    ignored_apps_flags = [
        flag for param, flag in _APPS_ONLY_DEPLOY_FLAGS
        if ms_ctx.get_parameter_source(param)
        == click.core.ParameterSource.COMMANDLINE
    ]
    if ignored_apps_flags:
        click.echo(
            "# WARNING: ignored with --target model-serving (only used by "
            f"--target apps): {', '.join(ignored_apps_flags)}",
            err=True,
        )
    # --version redeploys an existing logged version; --no-deploy skips the
    # deploy step. Combining them would be a no-op, so it's an error (#414).
    if version is not None and no_deploy:
        raise click.UsageError(
            "--version deploys an ALREADY-LOGGED version, but --no-deploy "
            "skips the deploy step — combined, nothing would happen. Drop "
            "one of the two."
        )
    config = _read_apx_agent_config()
    if model is None:
        raise click.UsageError(
            "--model is required when --target model-serving. "
            "Pass --target apps to use the Databricks Apps flow instead."
        )
    if registered_model_name is None:
        configured = config.get("registered_model")
        if isinstance(configured, str) and configured.strip():
            registered_model_name = configured.strip()
        else:
            raise click.UsageError(
                "--name is required when --target model-serving (or set "
                "[tool.apx.agent].registered_model in pyproject.toml). "
                "Pass --target apps to use the Databricks Apps flow instead."
            )
    if env_suffix:
        # Environment story (#407): a naming convention, not config
        # management — the suffixed UC short name flows through to the
        # endpoint/agent naming derived from it.
        registered_model_name = f"{registered_model_name}-{env_suffix}"
    effective_module = module or "agent:agent"

    if dry_run:
        _emit_serving_deploy_plan(
            uc_name=registered_model_name,
            model=model,
            module=effective_module,
            target_reason=target_reason,
            experiment=experiment or config.get("experiment"),
            publish_tools=publish_tools,
            no_deploy=no_deploy,
            readyz_gate=readyz_gate,
            set_uc_tags=set_uc_tags,
            version=version,
            scale_to_zero=scale_to_zero,
            workload_size=workload_size,
            json_output=json_output,
            timeout_seconds=timeout_seconds,
        )
        return

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

    def _say(msg: str) -> None:
        """Progress log: stdout normally, stderr under --json-output."""
        click.echo(msg, err=json_output)

    # Pre-flight: mode banner + secret scan. Both are logging concerns, so a
    # --version redeploy (nothing gets logged) skips them (#414).
    if version is None:
        _say(
            f"Logging with: env-var-capture="
            f"{'on' if effective_capture else 'off'}, secrets-scan=on",
        )
    suspicious = _run_secret_scan(Path.cwd()) if version is None else []
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
    effective_experiment = experiment or config.get("experiment")
    effective_agent_name = (
        agent_name
        or config.get("name")
        or registered_model_name.rsplit(".", 1)[-1]
    )
    resolved_experiment_id: str | None = None
    if effective_experiment:
        click.echo(f"# experiment: {effective_experiment}", err=True)

    # Sub-step ledger (#402/#405): every step lands in ok/failed/skipped, a
    # best-effort step failure is collected instead of swallowed, and the
    # command exits non-zero (with the failures named) if any step failed —
    # mirroring delete's best-effort-then-aggregate pattern.
    step_outcomes: dict[str, str] = {
        "publish_tools": "skipped",
        "log": "skipped",
        "deploy": "skipped",
        "gate": "skipped",
        "set_uc_tags": "skipped",
    }
    step_failures: list[str] = []
    published_tools: list[str] = []
    registered_version: str | None = None
    endpoint_name: str | None = None
    # uv.lock restore state (#416): set when _sanitize_uv_lock rewrites the
    # project's own lock in place, so a failed deploy puts the original back.
    lock_path = Path.cwd() / "uv.lock"
    original_lock: bytes | None = None

    def _orphans() -> str:
        """Name already-created resources so the operator knows what's left behind."""
        parts: list[str] = []
        if published_tools:
            parts.append(f"published UC tools: {', '.join(published_tools)}")
        if registered_version is not None:
            parts.append(
                f"registered model version: {registered_model_name} "
                f"v{registered_version}"
            )
        if not parts:
            return ""
        return (
            " Already-created resources (NOT rolled back — clean up manually "
            "if needed): " + "; ".join(parts) + "."
        )

    def _emit_failure(msg: str) -> NoReturn:
        # The deploy failed after sanitizing the project's own uv.lock in
        # place — put the original bytes back so the working tree isn't left
        # silently mutated (#416).
        if original_lock is not None:
            lock_path.write_bytes(original_lock)
            click.echo(
                "# restored original uv.lock (deploy failed after it was "
                "sanitized in place)",
                err=True,
            )
        if json_output:
            click.echo(json.dumps({
                "ok": False,
                "error": msg,
                "uc_name": registered_model_name,
                "version": registered_version,
                "endpoint": endpoint_name,
                "steps": step_outcomes,
            }))
            raise click.exceptions.Exit(1)
        raise click.ClickException(msg)

    def _fail(step: str, msg: str) -> NoReturn:
        step_outcomes[step] = "failed"
        _emit_failure(msg + _orphans())

    # 1. Publish @tool(uc=...) tools first so they exist in UC by the
    # time log_agent's resource collector picks them up. A --version
    # redeploy skips this: the version being promoted was logged (and its
    # tools published) by an earlier deploy (#414).
    if publish_tools and version is None:
        try:
            from apx_agent import publish_tools_to_uc
            results = publish_tools_to_uc(agent)
            step_outcomes["publish_tools"] = "ok"
            published_tools = [r.uc_name for r in results]
            if results:
                for r in results:
                    grants = ", ".join(r.grants_applied) or "none"
                    _say(f"  published {r.uc_name} (grants: {grants})")
            else:
                _say("  (no @tool(uc=...) decorated tools to publish)")
        except Exception as e:
            # Best-effort by design (log + deploy can still succeed without
            # the tools), but the failure is collected and fails the exit
            # code at the end instead of vanishing (#402).
            step_outcomes["publish_tools"] = "failed"
            step_failures.append(f"publish-tools: {e}")
            click.echo(f"# publish-tools failed: {e}", err=True)
            click.echo(
                "# continuing with log + deploy — this failure will still "
                "fail the command's exit code",
                err=True,
            )

    # MLflow captures the project's uv.lock into the logged model. Re-point its
    # internal Databricks PyPI proxy at public PyPI so the deployed serving
    # endpoint can install deps (it can't reach the dev proxy). This rewrites
    # the project's OWN uv.lock in place: kept on success (public-PyPI locks
    # are repo policy), restored by _emit_failure if the deploy fails (#416).
    if version is None:
        pre_sanitize_lock = lock_path.read_bytes() if lock_path.exists() else None
        if _sanitize_uv_lock(lock_path):
            original_lock = pre_sanitize_lock
            click.echo(
                "# rewrote uv.lock in place → public PyPI for the logged model "
                "(kept on success; restored if the deploy fails)",
                err=True,
            )
        _warn_unknown_lock_mirrors(lock_path)

    # 2. Log + register
    #
    # (Config knobs + persona overlay + declared tools are applied inside
    # log_agent via finalize_agent — no separate call needed here.)

    if version is not None:
        # --version redeploy (#414): the version already exists in UC with
        # its own provenance tags — go straight to the deploy step below.
        registered_version = str(version)
        _say(
            f"Redeploying already-logged {registered_model_name} version "
            f"{registered_version} (--version: publish_tools + log skipped)"
        )
    else:
        try:
            if effective_experiment:
                _exp = mlflow.set_experiment(effective_experiment)
                resolved_experiment_id = _exp.experiment_id
            with _EnvVarGuard(capture=effective_capture), mlflow.start_run():
                info = log_agent(
                    agent,
                    model=model,
                    registered_model_name=registered_model_name,
                    experiment=effective_experiment,
                )
        except Exception as e:
            _fail("log", f"log_agent failed: {e}.")
        step_outcomes["log"] = "ok"
        registered_version = info.registered_model_version
        _say(f"Logged {registered_model_name} version {registered_version}")

        # 2b. Provenance (issue #403): stamp git SHA / dirty flag / uv.lock hash on
        # the registered version — same tags the Apps manifest gets — so `apx-agent
        # doctor` can detect drift between what's deployed and the local tree.
        # Best-effort: a provenance write failure never turns a green deploy red.
        provenance_tags = _provenance_version_tags(Path.cwd())
        if provenance_tags:
            try:
                from mlflow.tracking import MlflowClient
                _prov_client = MlflowClient()
                for _prov_key, _prov_value in provenance_tags.items():
                    _prov_client.set_model_version_tag(
                        registered_model_name, str(registered_version),
                        _prov_key, _prov_value,
                    )
                _say(
                    f"  provenance: {len(provenance_tags)} tags written on "
                    f"version {registered_version}"
                )
            except Exception as e:
                click.echo(f"# provenance tags failed (non-fatal): {e}", err=True)

    # 3. Deploy to Model Serving
    if not no_deploy:
        try:
            from databricks import agents  # type: ignore[attr-defined]
        except ImportError as e:
            _fail(
                "deploy",
                "databricks-agents is required for deployment. "
                f"Install with: uv add databricks-agents ({e}).",
            )
        # Scale passthrough (#407): forwarded ONLY when set explicitly so
        # databricks.agents.deploy's own defaults stay in charge otherwise.
        deploy_kwargs: dict[str, Any] = {}
        if scale_to_zero is not None:
            deploy_kwargs["scale_to_zero"] = scale_to_zero
        if workload_size is not None:
            deploy_kwargs["workload_size"] = workload_size
        try:
            deployment = agents.deploy(
                registered_model_name, model_version=registered_version,
                **deploy_kwargs,
            )
        except Exception as e:
            _fail("deploy", f"databricks.agents.deploy failed: {e}.")
        step_outcomes["deploy"] = "ok"
        endpoint_name = getattr(deployment, "endpoint_name", None) or registered_model_name.rsplit(".", 1)[-1]
        _say(
            f"Deployed {registered_model_name} version "
            f"{registered_version} as a serving endpoint."
        )
        _say(f"  Endpoint: {endpoint_name}")
        _say("  View in workspace: Serving → Endpoints")

        # 3b. Health gate (#406): a green deploy must mean "the agent
        # answers", not "endpoint update accepted". Poll the endpoint to
        # READY, then send one smoke invocation — the model-serving mirror
        # of the apps /readyz gate, behind the same flag.
        if readyz_gate:
            assert endpoint_name is not None  # set just above from the deploy
            gate_error = _serving_health_gate(
                endpoint_name, log=_say, timeout_seconds=timeout_seconds,
            )
            if gate_error is None:
                step_outcomes["gate"] = "ok"
                _say("  health gate: endpoint READY + smoke invocation answered")
            else:
                _fail(
                    "gate",
                    f"health gate failed — {registered_model_name} version "
                    f"{registered_version} deployed to endpoint "
                    f"{endpoint_name!r} but did not answer: {gate_error} "
                    f"The endpoint update WAS accepted, so version "
                    f"{registered_version} is live-but-unverified — roll "
                    "forward with a new deploy or roll the endpoint back to "
                    "the previous version. Re-run with --no-readyz-gate to "
                    "accept the deploy without the health check.",
                )
        else:
            _say(
                "# --no-readyz-gate: skipping endpoint readiness poll + "
                "smoke invocation"
            )
    else:
        _say("Skipping deploy (--no-deploy).")

    # 4. Set UC tags so the agent shows up in apx-agent list / topology / watchdog
    if set_uc_tags:
        try:
            from apx_agent import set_uc_tags_for_agent
            written = set_uc_tags_for_agent(
                agent,
                registered_model_name=registered_model_name,
                model=model,
                name=effective_agent_name,
                experiment_id=resolved_experiment_id,   # lets `apx-agent label` resolve traces without --experiment
            )
            step_outcomes["set_uc_tags"] = "ok"
            if written:
                _say(f"  {len(written)} apx.agent.* tags written on {registered_model_name} "
                     f"(agent_name={effective_agent_name})")
            else:
                click.echo(
                    f"# set-uc-tags: 0 tags written on {registered_model_name} "
                    f"— check UC permissions on the registered model (see warnings)",
                    err=True,
                )
        except Exception as e:
            # Collected, not swallowed: an untagged agent silently never
            # shows up in list / topology / watchdog (#402).
            step_outcomes["set_uc_tags"] = "failed"
            step_failures.append(f"set-uc-tags: {e}")
            click.echo(f"# set-uc-tags failed: {e}", err=True)

    # 5. Aggregate: a "green" deploy must not hide failed sub-steps (#402).
    if step_failures:
        _emit_failure(
            f"deploy completed with {len(step_failures)} failed sub-step(s): "
            + "; ".join(step_failures) + "." + _orphans()
        )
    if json_output:
        summary: dict[str, Any] = {
            "ok": True,
            "uc_name": registered_model_name,
            "version": registered_version,
            "endpoint": endpoint_name,
            "steps": step_outcomes,
        }
        # Present only when set explicitly — mirrors what was forwarded.
        if scale_to_zero is not None:
            summary["scale_to_zero"] = scale_to_zero
        if workload_size is not None:
            summary["workload_size"] = workload_size
        click.echo(json.dumps(summary))


def _emit_apps_deploy_plan(
    *,
    module: str,
    target_reason: str,
    bundle_target: str,
    no_run: bool,
    auto_build_wheel: bool,
    auto_experiment: bool,
    readyz_gate: bool,
    register_uc: bool,
    uc_name: str | None,
    vars: tuple[str, ...],
    env_pairs: tuple[str, ...],
    secret_env_pairs: tuple[str, ...],
    json_output: bool,
    timeout_seconds: int,
    readyz_retries: int,
) -> None:
    """Print the ``--target apps`` deploy plan WITHOUT mutating anything (#414).

    Reads only local files (databricks.yml / pyproject.toml / git metadata):
    no ``databricks`` CLI calls, no databricks.yml writes (``--env`` /
    ``--secret-env`` are reported as key names, never merged), no network.
    """
    cwd = Path.cwd()
    doc = _read_databricks_yml(cwd)
    bundle_key, app_name = _resolve_app_name(doc)
    resolved_uc = _resolve_apps_uc_name(
        _read_apx_agent_config(), app_name, override=uc_name,
    )

    # Key NAMES that would merge into databricks.yml — parsed for validation
    # (a malformed pair should fail the dry run too), values never echoed.
    env_keys = [_parse_env_flag(e).name for e in env_pairs]
    secret_env_keys = [_parse_secret_env_flag(s).name for s in secret_env_pairs]

    # Vars that would pass to bundle deploy + run, mirroring the real flow's
    # gating: an auto-resolved experiment id (placeholder — resolving it for
    # real needs the workspace) and the apx_git_sha correlation var when the
    # bundle declares it (#404).
    plan_vars = list(vars)
    if any(v.startswith("mlflow_experiment_id=") for v in plan_vars):
        experiment = "from --var mlflow_experiment_id="
    elif auto_experiment:
        experiment = (
            f"auto-resolve /Users/<current-user>/{app_name}-{bundle_target} "
            "at deploy time (created if missing)"
        )
        plan_vars.append("mlflow_experiment_id=<auto-resolved>")
    else:
        experiment = "none (--no-auto-experiment, no --var mlflow_experiment_id=)"
    from ._apps_registry import GIT_SHA_TAG
    correlation_sha = _provenance_version_tags(cwd).get(GIT_SHA_TAG)
    declared_bundle_vars = doc.get("variables")
    if (
        correlation_sha
        and isinstance(declared_bundle_vars, dict)
        and "apx_git_sha" in declared_bundle_vars
        and not any(v.startswith("apx_git_sha=") for v in vars)
    ):
        plan_vars.append(f"apx_git_sha={correlation_sha}")

    steps: dict[str, str] = {
        "validate": "run",
        "deploy": "run",
        "run": "skipped (--no-run)" if no_run else "run",
        "poll": (
            "skipped (--no-run)" if no_run
            else f"run (timeout {timeout_seconds}s)"
        ),
        "readyz": (
            "skipped (--no-run)" if no_run
            else f"run ({readyz_retries} attempts)" if readyz_gate
            else "skipped (--no-readyz-gate)"
        ),
        "register_uc": (
            "skipped (--no-register-uc)" if not register_uc
            else "run" if resolved_uc
            else "skipped (no UC name resolves)"
        ),
    }

    if json_output:
        click.echo(json.dumps({
            "plan": True,
            "target": "apps",
            "target_reason": target_reason,
            "module": module,
            "bundle_target": bundle_target,
            "app_name": app_name,
            "bundle_key": bundle_key,
            "uc_name": resolved_uc,
            "auto_build_wheel": auto_build_wheel,
            "experiment": experiment,
            "env_keys": env_keys,
            "secret_env_keys": secret_env_keys,
            "bundle_vars": plan_vars,
            "steps": steps,
        }))
        return
    click.echo("# DRY RUN — plan only; nothing validated, written, or deployed")
    click.echo(f"target: apps ({target_reason})")
    click.echo(f"module: {module}")
    click.echo(f"bundle_target: {bundle_target}")
    click.echo(f"app_name: {app_name} (bundle key: {bundle_key})")
    click.echo(f"uc_name: {resolved_uc or '<none — UC registration would skip>'}")
    click.echo(f"wheel auto-build: {'on' if auto_build_wheel else 'off'}")
    click.echo(f"experiment: {experiment}")
    click.echo(
        "env keys to merge (names only): " + (", ".join(env_keys) or "(none)")
    )
    click.echo(
        "secret env keys to merge (names only): "
        + (", ".join(secret_env_keys) or "(none)")
    )
    click.echo("bundle vars: " + (", ".join(plan_vars) or "(none)"))
    click.echo("steps:")
    for step, disposition in steps.items():
        click.echo(f"  {step}: {disposition}")


def _emit_serving_deploy_plan(
    *,
    uc_name: str,
    model: str,
    module: str,
    target_reason: str,
    experiment: Any,
    publish_tools: bool,
    no_deploy: bool,
    readyz_gate: bool,
    set_uc_tags: bool,
    version: int | None,
    scale_to_zero: bool | None,
    workload_size: str | None,
    json_output: bool,
    timeout_seconds: int,
) -> None:
    """Print the ``--target model-serving`` deploy plan WITHOUT mutating (#414).

    No ``databricks.agents`` import, no MLflow calls, no uv.lock sanitize,
    no network — the UC name shown is fully resolved (config fallback +
    ``--env-suffix`` already applied by the caller).
    """
    scale_kwargs: dict[str, Any] = {}
    if scale_to_zero is not None:
        scale_kwargs["scale_to_zero"] = scale_to_zero
    if workload_size is not None:
        scale_kwargs["workload_size"] = workload_size
    redeploy = version is not None
    steps: dict[str, str] = {
        "publish_tools": (
            "skipped (--version redeploy)" if redeploy
            else "run" if publish_tools
            else "skipped (--no-publish-tools)"
        ),
        "log": "skipped (--version redeploy)" if redeploy else "run",
        "deploy": "skipped (--no-deploy)" if no_deploy else "run",
        "gate": (
            "skipped (--no-deploy)" if no_deploy
            else f"run (timeout {timeout_seconds}s)" if readyz_gate
            else "skipped (--no-readyz-gate)"
        ),
        "set_uc_tags": "run" if set_uc_tags else "skipped (--no-set-uc-tags)",
    }
    if json_output:
        click.echo(json.dumps({
            "plan": True,
            "target": "model-serving",
            "target_reason": target_reason,
            "module": module,
            "uc_name": uc_name,
            "model": model,
            "experiment": experiment,
            "version": version,
            "scale_kwargs": scale_kwargs,
            "steps": steps,
        }))
        return
    click.echo("# DRY RUN — plan only; nothing logged, deployed, or written")
    click.echo(f"target: model-serving ({target_reason})")
    click.echo(f"module: {module}")
    click.echo(f"uc_name: {uc_name}")
    click.echo(f"model: {model}")
    click.echo(f"experiment: {experiment or '(MLflow default)'}")
    if redeploy:
        click.echo(f"version: {version} (already logged — redeploy)")
    click.echo(
        "scale kwargs forwarded: "
        + (
            json.dumps(scale_kwargs) if scale_kwargs
            else "(none — databricks.agents.deploy defaults apply)"
        )
    )
    click.echo("steps:")
    for step, disposition in steps.items():
        click.echo(f"  {step}: {disposition}")


def _serving_health_gate(
    endpoint_name: str,
    *,
    log: Any,
    timeout_seconds: int = 300,
) -> str | None:
    """Post-deploy health gate for ``--target model-serving`` (#406).

    Polls ``ws.serving_endpoints.get(endpoint_name)`` until the endpoint
    reports ``state.ready == READY`` with no config update in progress
    (bounded by ``timeout_seconds``, exponential backoff capped at 15s,
    fast-failing on a terminal UPDATE_FAILED / UPDATE_CANCELED — the
    serving mirror of ``_poll_app_ready``), then sends ONE minimal smoke
    invocation through ``ws.serving_endpoints.query``. Success is any
    non-error structured response: the agent parsed a request and answered.

    Returns ``None`` when the endpoint answers, or an error-detail string
    otherwise. Never raises — the CALLER owns the failure contract.
    """
    try:
        from databricks.sdk import WorkspaceClient
        ws = WorkspaceClient()
    except Exception as exc:
        return f"could not build a workspace client to verify the endpoint: {exc}."

    log(f"# health gate: polling endpoint {endpoint_name!r} for READY")
    deadline = time.time() + timeout_seconds
    delay = 1.0
    last_state: tuple[str, str] = ("", "")
    while True:
        try:
            info = ws.serving_endpoints.get(endpoint_name)
        except Exception as exc:
            # The endpoint may not be visible yet right after deploy —
            # treat get() errors as transient until the deadline.
            log(f"  (serving_endpoints.get failed: {exc}; retrying)")
        else:
            state = getattr(info, "state", None)
            raw_ready = getattr(state, "ready", None)
            raw_update = getattr(state, "config_update", None)
            ready = (
                str(getattr(raw_ready, "value", raw_ready)).upper()
                if raw_ready is not None else ""
            )
            update = (
                str(getattr(raw_update, "value", raw_update)).upper()
                if raw_update is not None else ""
            )
            if (ready, update) != last_state:
                log(f"  status: ready={ready or '?'} config_update={update or '?'}")
                last_state = (ready, update)
            if update in ("UPDATE_FAILED", "UPDATE_CANCELED"):
                return (
                    f"endpoint {endpoint_name!r} config update ended in "
                    f"{update}. Inspect the build with `apx-agent logs "
                    f"--endpoint {endpoint_name} --build`."
                )
            if ready == "READY" and update in ("", "NOT_UPDATING"):
                break

        if time.time() >= deadline:
            return (
                f"timed out after {timeout_seconds}s waiting for endpoint "
                f"{endpoint_name!r} to reach READY (last observed: "
                f"ready={last_state[0] or '?'} "
                f"config_update={last_state[1] or '?'})."
            )
        time.sleep(min(delay, max(0.0, deadline - time.time())))
        delay = min(delay * 1.5, 15.0)

    log(f"# health gate: smoke invocation on {endpoint_name!r}")
    try:
        from databricks.sdk.service.serving import ChatMessage, ChatMessageRole
        response = ws.serving_endpoints.query(
            name=endpoint_name,
            messages=[ChatMessage(role=ChatMessageRole.USER, content="ping")],
        )
    except Exception as exc:
        return (
            f"endpoint {endpoint_name!r} is READY but the smoke invocation "
            f"failed: {exc}."
        )
    if response is None:
        return f"endpoint {endpoint_name!r} returned an empty smoke response."
    return None


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


def _git_head_sha(cwd: Path) -> str | None:
    """Return the full HEAD commit SHA of the git repo at ``cwd``, or None.

    Best-effort provenance capture (P1): returns None when ``cwd`` is not a git
    repo, git isn't installed, or the call fails — callers treat a missing SHA
    as "provenance unavailable" rather than an error.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _git_is_dirty(cwd: Path) -> bool:
    """Return True iff the git tree at ``cwd`` has uncommitted changes.

    Conservative: returns True on any error (can't confirm clean → treat as
    dirty), so the promote gate refuses rather than risk shipping unsoaked
    changes.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def _provenance_version_tags(cwd: Path) -> dict[str, str]:
    """Best-effort deploy-provenance tags for the tree at ``cwd`` (issue #403).

    Returns ``apx.apps.git_sha`` + ``apx.git_dirty`` ("true"/"false") when
    ``cwd`` is a git repo, and ``apx.lock_sha256`` (sha256 of ``uv.lock``)
    when a lock file exists. Never raises: outside a git repo the git tags
    are simply omitted, an unreadable lock omits the lock tag — provenance
    capture must never fail a deploy.
    """
    import hashlib

    from ._apps_registry import GIT_DIRTY_TAG, GIT_SHA_TAG, LOCK_SHA256_TAG

    tags: dict[str, str] = {}
    sha = _git_head_sha(cwd)
    if sha:
        tags[GIT_SHA_TAG] = sha
        tags[GIT_DIRTY_TAG] = "true" if _git_is_dirty(cwd) else "false"
    try:
        lock_bytes = (cwd / "uv.lock").read_bytes()
    except OSError:
        lock_bytes = None  # missing/unreadable lock → tag omitted
    if lock_bytes is not None:
        tags[LOCK_SHA256_TAG] = hashlib.sha256(lock_bytes).hexdigest()
    return tags


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


def _resolve_app_name(bundle_doc: dict[str, Any]) -> _AppNameResolution:
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
    return _AppNameResolution(bundle_key=bundle_key, app_name=app_name)


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
    if src_ver and expected_name and src_ver not in expected_name:
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
            _warn_unknown_lock_mirrors(lockfile)
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
    # Always read from the SOURCE pyproject.toml when wheel_path is set.
    # A stale .build/pyproject.toml from a prior deploy already has a .whl
    # path, causing has_editable=False and skipping the rewrite — leaving
    # the old wheel version pinned indefinitely.
    source_pyproject = build_dir.parent / "pyproject.toml"
    if source_pyproject.exists():
        text = source_pyproject.read_text()
    elif pyproject.exists():
        text = pyproject.read_text()
    else:
        return
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
    _warn_unknown_lock_mirrors(lockfile)


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
) -> _ReadyzResult:
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
        return _ReadyzResult(is_ready=False, checks={"error": f"could not mint token: {str(exc)[:120]}"})
    if not token:
        return _ReadyzResult(is_ready=False, checks={"error": "could not mint databricks auth token"})

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
                return _ReadyzResult(is_ready=data.get("status") == "ready", checks=checks)

        # Unreachable / unparseable — back off and retry.
        if attempt < max(1, attempts) - 1 and delay_s > 0:
            _time.sleep(delay_s)

    return _ReadyzResult(is_ready=False, checks={"error": f"readyz unreachable: {last_error}"})


def _fetch_app_log_tail(app_name: str, *, profile: str | None, lines: int = 40) -> str:
    """Return the last *lines* of app stdout/stderr, or a fallback message on error.

    Never raises — called from an error path where we want best-effort context.
    """
    try:
        cmd = ["databricks", "apps", "logs", app_name]
        if profile:
            cmd.extend(["--profile", profile])
        import subprocess as _sp
        proc = _sp.run(cmd, check=False, capture_output=True, text=True, timeout=15)
        raw = (proc.stdout or "") + (proc.stderr or "")
        tail = [ln for ln in raw.splitlines() if ln.strip()][-lines:]
        if tail:
            return "\n".join(f"  {ln}" for ln in tail)
        return "  (no log output returned)"
    except Exception as exc:
        return f"  (could not fetch logs: {exc})"


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
        msg = (
            "Pre-flight failed for --target apps. Missing in current "
            f"directory: {', '.join(missing)}."
        )
        if (cwd / "app.py").exists() or (cwd / "agent.py").exists():
            # ADK-style / model-serving lookalike misrouted to apps by the
            # catch-all default — the likely fix is the other target, not a
            # rescaffold.
            msg += (
                " This directory looks like a model-serving layout (agent.py/"
                "app.py present) — the likely fix is re-running with "
                "--target model-serving."
            )
        else:
            msg += (
                " Run `apx-agent scaffold <name> --target apps` to generate "
                "the expected layout."
            )
        raise click.ClickException(msg)


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
            "the eval extra: uv add 'apx-agent[eval]'. "
            f"(underlying error: {e})"
        ) from e


_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _parse_env_flag(raw: str) -> _EnvPair:
    """Parse one ``--env KEY=VALUE`` pair."""
    key, sep, value = raw.partition("=")
    if not sep or not _ENV_NAME_RE.fullmatch(key):
        raise click.ClickException(
            f"--env expects KEY=VALUE with an env-var-shaped KEY "
            f"([A-Za-z_][A-Za-z0-9_]*), got: {raw!r}"
        )
    return _EnvPair(name=key, value=value)


def _parse_secret_env_flag(raw: str) -> _SecretEnvRef:
    """Parse one ``--secret-env KEY=scope/key`` pair.

    The value side is a Databricks secret REFERENCE (scope/key), never the
    secret value itself.
    """
    key, sep, ref = raw.partition("=")
    scope, slash, secret_key = ref.partition("/")
    if (
        not sep or not slash or not scope or not secret_key
        or not _ENV_NAME_RE.fullmatch(key)
    ):
        raise click.ClickException(
            f"--secret-env expects KEY=scope/key (a Databricks secret "
            f"reference — never the secret value), got: {raw!r}"
        )
    return _SecretEnvRef(name=key, scope=scope, key=secret_key)


def _merge_env_into_databricks_yml(
    cwd: Path,
    *,
    bundle_key: str,
    env_pairs: list[_EnvPair],
    secret_env_pairs: list[_SecretEnvRef],
    log: Any,
) -> _EnvMergeResult:
    """Merge --env / --secret-env entries into ``resources.apps.<key>.config.env``.

    Same contract as ``_auto_update_databricks_yml``: the merge PERSISTS in
    databricks.yml, and an entry whose ``name`` already exists in
    ``config.env`` is NEVER clobbered — it is skipped with a warning.

    ``--secret-env`` additionally declares a Databricks secret app resource
    (permission READ) and points the env entry at it via ``valueFrom``; only
    the scope/key reference is written, never a secret value. If a resource
    with the derived name already exists it is reused, not clobbered.
    """
    import yaml

    path = cwd / "databricks.yml"
    doc = _read_databricks_yml(cwd)
    # resources.apps.<bundle_key> presence was already validated by
    # _resolve_app_name on this same document.
    app_block = doc["resources"]["apps"][bundle_key]
    config = app_block.setdefault("config", {})
    env_list = config.setdefault("env", [])
    existing_env = {
        e["name"] for e in env_list
        if isinstance(e, dict) and isinstance(e.get("name"), str)
    }

    env_added: list[str] = []
    secret_added: list[str] = []
    skipped: list[str] = []

    for name, value in env_pairs:
        if name in existing_env:
            skipped.append(name)
            continue
        env_list.append({"name": name, "value": value})
        existing_env.add(name)
        env_added.append(name)

    resources: list[dict[str, Any]] = app_block.setdefault("resources", [])
    resource_names: set[str] = set()
    for entry in resources:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("name"), str):
            resource_names.add(entry["name"])
        for v in entry.values():
            if isinstance(v, dict) and isinstance(v.get("name"), str):
                resource_names.add(v["name"])

    for name, scope, secret_key in secret_env_pairs:
        if name in existing_env:
            skipped.append(name)
            continue
        res_name = "secret-" + name.lower().replace("_", "-")
        if res_name not in resource_names:
            resources.append({
                "name": res_name,
                "description": (
                    f"Secret backing the {name} env var "
                    "(added by apx-agent deploy --secret-env)."
                ),
                "secret": {
                    "scope": scope,
                    "key": secret_key,
                    "permission": "READ",
                },
            })
            resource_names.add(res_name)
        env_list.append({"name": name, "valueFrom": res_name})
        existing_env.add(name)
        secret_added.append(name)

    if env_added or secret_added:
        path.write_text(
            yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)
        )

    if env_added:
        log(f"  env → config.env: {', '.join(env_added)} (values not echoed)")
    if secret_added:
        log("  secret-env → config.env (valueFrom secret reference): "
            f"{', '.join(secret_added)}")
    if skipped:
        log("  WARNING: skipped --env/--secret-env entries already declared "
            "in config.env (never clobbered — edit databricks.yml to "
            f"change them): {', '.join(skipped)}")
    return _EnvMergeResult(
        env_added=env_added, secret_added=secret_added, skipped=skipped,
    )


def _auto_update_databricks_yml(
    cwd: Path,
    *,
    agent: Any,
    bundle_key: str,
    log: Any,
) -> _BundleUpdateResult:
    """Merge missing ResourceSpec entries into databricks.yml.

    Returns added_names and skipped_names. The bundle document is read,
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
    return _BundleUpdateResult(added=added, skipped=skipped)


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


def _resolve_apps_uc_name(
    config: dict[str, Any], app_name: str, *, override: str | None = None,
) -> str | None:
    """Resolve the UC three-part name to register an Apps version manifest under.

    Resolution order, first hit wins:

      1. ``override`` — the ``--uc-name`` flag.
      2. ``[tool.apx.agent].registered_model`` in pyproject.
      3. ``<catalog>.<schema>.<app_name>`` composed from the config's catalog +
         schema (top-level or under ``template``), with the App name sanitized
         into a UC-legal identifier (hyphens → underscores).

    Returns ``None`` when none resolve — the caller treats that as "skip
    registration with a loud, actionable notice", not an error: a bare
    ``apx agents deploy --target apps`` must still succeed. Placeholder
    catalog/schema values (``$CATALOG`` / ``$SCHEMA`` from a fresh scaffold)
    are treated as absent so an unconfigured project skips rather than
    registering under a literal ``$CATALOG.$SCHEMA.x``.
    """
    if override:
        return override
    registered = config.get("registered_model")
    if isinstance(registered, str) and registered.strip():
        return registered.strip()

    _tmpl = config.get("template")
    template: dict[str, Any] = _tmpl if isinstance(_tmpl, dict) else {}
    catalog = config.get("catalog") or template.get("catalog")
    schema = config.get("schema") or template.get("schema")
    if (
        isinstance(catalog, str) and catalog and not catalog.startswith("$")
        and isinstance(schema, str) and schema and not schema.startswith("$")
    ):
        model_id = app_name.replace("-", "_")
        return f"{catalog}.{schema}.{model_id}"
    return None


def _resolve_project_uc_name(cwd: Path) -> str | None:
    """Resolve the UC name the current project's deploys register under, or None.

    Shared by ``agents status`` and doctor's deploy-provenance check so both
    answer "which registered model is THIS project's deploy?" identically:
    an Apps bundle name (``databricks.yml``) composed via
    ``_resolve_apps_uc_name``, else ``[tool.apx.agent].registered_model``.
    """
    config = _read_apx_agent_config(cwd / "pyproject.toml")
    try:
        _bundle_key, app_name = _resolve_app_name(_read_databricks_yml(cwd))
    except Exception:
        app_name = None  # not an Apps bundle — registered_model may still resolve
    if app_name:
        return _resolve_apps_uc_name(config, app_name)
    registered = config.get("registered_model")
    if isinstance(registered, str) and registered.strip():
        return registered.strip()
    return None


def _register_apps_manifest_step(
    *,
    module: str,
    config: dict[str, Any],
    app_name: str,
    bundle_target: str,
    uc_name_override: str | None,
    extra_version_tags: dict[str, str] | None = None,
    profile: str | None = None,  # only names the repair command on failure
    log: Any,
) -> str | None:
    """Register a UC version manifest for a freshly-deployed App (best-effort).

    Runs AFTER the App is live. Skips with a loud, actionable stderr notice when
    a UC name or LLM model can't be resolved (so a bare apps deploy still
    succeeds). Treats any logging/registration failure as non-fatal — the App is
    already running; a missing manifest must not turn a green deploy red.

    Returns the registered manifest version, or None when registration was
    skipped or failed (issue #417 — the deploy JSON reports what shipped).
    """
    uc_name = _resolve_apps_uc_name(config, app_name, override=uc_name_override)
    if uc_name is None:
        log(
            "# UC registration skipped: no UC model name resolved. Set "
            "[tool.apx.agent].registered_model (or a non-placeholder "
            "catalog + schema), or pass --uc-name, to enable the Apps version "
            "ledger (discovery in `apx agents list` / topology / watchdog). "
            "Pass --no-register-uc to silence this."
        )
        return None

    model = config.get("model")
    if not model:
        log(
            "# UC registration skipped: no LLM model in [tool.apx.agent].model "
            f"to log {uc_name} with. Set a model, or pass --no-register-uc to "
            "silence this."
        )
        return None

    agent_name = config.get("name") or app_name
    try:
        agent = _load_finalized_agent(module)
        from ._apps_registry import register_apps_manifest
        res = register_apps_manifest(
            agent,
            uc_name=uc_name,
            model=model,
            app_name=app_name,
            bundle_target=bundle_target,
            agent_name=agent_name,
            extra_version_tags=extra_version_tags,
        )
        log(
            f"# registered {res.uc_name} version {res.version} "
            f"(manifest for App {res.app_name}; not promoted to serving)"
        )
        return res.version
    except Exception as e:  # non-fatal: the App is live regardless
        # Name the exact single-agent repair command (issue #418) so the
        # operator doesn't reach for a workspace-wide `fleet backfill`.
        repair = "apx-agent agents register"
        if uc_name_override:
            repair += f" --uc-name {uc_name_override}"
        if profile:
            repair += f" --profile {profile}"
        log(
            f"# UC registration failed (non-fatal — App is live): {e}\n"
            "# the App is running; only the version-ledger entry is missing. "
            f"Backfill it with:\n#   {repair}"
        )
        return None


def _apps_readyz_recovery_hint(
    app_name: str,
    *,
    profile: str | None,
    uc_name_override: str | None,
    log: Any,
) -> str:
    """Best-effort recovery hint for a failed readyz gate (issue #401).

    If a prior ``@prod`` UC version with recorded ``apx.apps.git_sha``
    provenance exists, name the exact ``canary rollback`` command that restores
    it; otherwise say plainly that re-deploying a known-good commit is the
    path. Never raises — a hint-lookup failure must not mask the readyz
    failure itself.
    """
    try:
        config = _read_apx_agent_config()
        uc_name = _resolve_apps_uc_name(config, app_name, override=uc_name_override)
        if uc_name is not None:
            from ._apps_registry import get_prod_alias_version, get_version_git_sha
            prod_version = get_prod_alias_version(uc_name)
            if prod_version and get_version_git_sha(uc_name, prod_version):
                cmd = (
                    "apx-agent canary rollback --target apps "
                    f"--to-version {prod_version}"
                )
                if profile:
                    cmd += f" --profile {profile}"
                return (
                    f"Recovery: prior @prod version {prod_version} of {uc_name} "
                    f"has recorded provenance — restore it with:\n  {cmd}"
                )
    except Exception as e:  # never mask the readyz failure with a hint failure
        log(f"# recovery-hint lookup failed (non-fatal): {e}")
    return (
        "Recovery: no prior @prod version with recorded provenance found — "
        "re-deploy a known-good commit to replace the broken app."
    )


@contextlib.contextmanager
def _json_cli_errors(as_json: bool) -> Iterator[None]:
    """Mirror the deploy ``--json-output`` failure contract for other
    mutation commands (issue #417): under ``--json``, a ``ClickException``
    (incl. ``UsageError``) becomes a single ``{"ok": false, "error": ...}``
    object on stdout + exit 1, instead of click's stderr rendering. Without
    the flag, the exception propagates unchanged.
    """
    try:
        yield
    except click.ClickException as e:
        if not as_json:
            raise
        click.echo(json.dumps({"ok": False, "error": str(e)}))
        raise click.exceptions.Exit(1) from e


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
    env_pairs: tuple[str, ...] = (),
    secret_env_pairs: tuple[str, ...] = (),
    json_output: bool,
    readyz_gate: bool = True,
    register_uc: bool = True,
    uc_name: str | None = None,
    app_name_override: str | None = None,
    poll_timeout_seconds: int = 300,
    readyz_attempts: int = 5,
) -> None:
    """Implement ``apx-agent deploy --target apps``.

    Routes all progress logs to stderr (so ``--json-output`` can keep stdout
    clean), runs the bundle validate → deploy → run → poll-ready sequence,
    registers a UC version manifest (unless ``--no-register-uc``), and prints
    either the app URL (default) or a single JSON summary (``--json-output``) at
    the end.

    Every deploy through here stamps provenance tags (git SHA, dirty flag,
    uv.lock hash) on the registered manifest version (issue #403). The canary /
    prod-at-commit paths call ``_deploy_apps_impl`` directly and pass their own
    ``apx.apps.git_sha``, so there is no double-set.
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
            env_pairs=env_pairs,
            secret_env_pairs=secret_env_pairs,
            json_output=json_output, readyz_gate=readyz_gate,
            register_uc=register_uc, uc_name=uc_name,
            app_name_override=app_name_override,
            extra_version_tags=_provenance_version_tags(cwd),
            poll_timeout_seconds=poll_timeout_seconds,
            readyz_attempts=readyz_attempts,
            log=log,
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
    env_pairs: tuple[str, ...] = (),
    secret_env_pairs: tuple[str, ...] = (),
    json_output: bool,
    readyz_gate: bool = True,
    register_uc: bool = True,
    uc_name: str | None = None,
    app_name_override: str | None = None,
    extra_version_tags: dict[str, str] | None = None,
    poll_timeout_seconds: int = 300,
    readyz_attempts: int = 5,
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
    bundle_key, resolved_app_name = _resolve_app_name(doc)
    app_name = app_name_override or resolved_app_name
    if app_name_override and app_name_override != resolved_app_name:
        log(f"# app-name override: polling {app_name} (target {bundle_target})")
    if bundle_key != resolved_app_name:
        log(f"# resolved bundle_key={bundle_key} app_name={resolved_app_name}")
    else:
        log(f"# resolved app_name: {resolved_app_name}")

    # 2. Optional auto-merge resources
    if auto_update_yml:
        log("# auto-update-yml: merging agent ResourceSpec into databricks.yml")
        agent = _load_finalized_agent(module)
        _auto_update_databricks_yml(
            cwd, agent=agent, bundle_key=bundle_key, log=log,
        )

    # 2a. --env / --secret-env (issue #415): merge caller-supplied runtime
    # env into resources.apps.<key>.config.env before the bundle deploy.
    # The merge PERSISTS in databricks.yml; existing entries are never
    # clobbered. Only key NAMES are logged — never values.
    injected_env_keys: list[str] = []
    injected_secret_env_keys: list[str] = []
    if env_pairs or secret_env_pairs:
        parsed_env = [_parse_env_flag(e) for e in env_pairs]
        parsed_secret = [_parse_secret_env_flag(s) for s in secret_env_pairs]
        log("# env config: merging --env/--secret-env into databricks.yml "
            "(the merge persists in the file)")
        merge = _merge_env_into_databricks_yml(
            cwd, bundle_key=bundle_key,
            env_pairs=parsed_env, secret_env_pairs=parsed_secret, log=log,
        )
        injected_env_keys = merge.env_added
        injected_secret_env_keys = merge.secret_added

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

    # 2d. Version correlation (issue #404): pass the deploy commit into the
    # app container as APX_GIT_SHA so every trace carries an apx.git_sha tag
    # and `canary analyze --target apps` can attribute traffic per version.
    # The sha is read from the provenance tags already computed for this
    # deploy (single source — never recomputed). Only injected when the
    # bundle declares the `apx_git_sha` variable (new scaffolds do), so
    # pre-#404 bundles keep deploying untouched; an explicit caller
    # `--var apx_git_sha=` always wins.
    from ._apps_registry import GIT_SHA_TAG
    correlation_sha = (extra_version_tags or {}).get(GIT_SHA_TAG)
    declared_bundle_vars = doc.get("variables")
    if (
        correlation_sha
        and isinstance(declared_bundle_vars, dict)
        and "apx_git_sha" in declared_bundle_vars
        and not any(v.startswith("apx_git_sha=") for v in vars or ())
    ):
        extra_vars.append(f"apx_git_sha={correlation_sha}")
        log(f"  version correlation: APX_GIT_SHA={correlation_sha[:12]} → app env")

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

    # 6. Poll for ACTIVE/RUNNING via `databricks apps get`. Skipped with
    # --no-run (issue #413): the app was never started, so polling would just
    # burn the timeout, and the readyz gate below self-skips on the empty
    # app_url. The UC manifest registration (6d) still runs — it records
    # intent/version and doesn't need the app up.
    app_url = ""
    if no_run:
        log("# --no-run: app not started — skipping readiness poll + readyz gate")
    else:
        log(f"# polling `databricks apps get {app_name}` for ACTIVE/RUNNING")
        payload = _poll_app_ready(
            app_name, profile, timeout_seconds=poll_timeout_seconds, log=log,
        )
        app_url = payload.get("url") or ""

        # 6b. Grant the app's service principal access to the tracing
        # experiment. The experiment is created under the deploying user, but
        # the app runs as its SP — without this grant every span is dropped.
        # Best-effort.
        sp = payload.get("service_principal_client_id")
        if sp and resolved_exp_id:
            if _grant_experiment_to_sp(resolved_exp_id, sp, profile=profile):
                log("  granted app SP CAN_MANAGE on tracing experiment")

    # 6c. readyz gate: prove the app actually answers + traces, not just that
    # the container booted. A green deploy should mean "the agent works".
    # The check dict is kept for the JSON summary (issue #417); it stays None
    # when the gate is off or --no-run left the app without a URL.
    readyz_checks: dict[str, Any] | None = None
    if readyz_gate and app_url:
        log(f"# readyz gate: GET {app_url}/readyz")
        ok, checks = _check_readyz(app_url, profile=profile, attempts=readyz_attempts)
        readyz_checks = checks
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
            log("# fetching app logs for crash context...")
            log_tail = _fetch_app_log_tail(app_name, profile=profile)
            logs_cmd = f"databricks apps logs {app_name}"
            if profile:
                logs_cmd += f" --profile {profile}"
            # Ledger the failed deploy (issue #401): the broken app IS live,
            # so the UC version trail must record it — tagged failed so no
            # one mistakes it for a healthy version. Best-effort, like the
            # success-path registration.
            if register_uc:
                _register_apps_manifest_step(
                    module=module,
                    config=_read_apx_agent_config(),
                    app_name=app_name,
                    bundle_target=bundle_target,
                    uc_name_override=uc_name,
                    extra_version_tags={
                        **(extra_version_tags or {}),
                        "apx.apps.readyz": "failed",
                    },
                    profile=profile,
                    log=log,
                )
            recovery = _apps_readyz_recovery_hint(
                app_name, profile=profile, uc_name_override=uc_name, log=log,
            )
            msg = (
                f"readyz gate failed — the app is STILL LIVE, serving in a "
                f"failed state:\n"
                f"  app: {app_name}\n"
                f"  url: {app_url}\n"
                f"{detail}\n\n"
                f"App logs (last 40 lines):\n"
                f"{'─' * 60}\n"
                f"{log_tail}\n"
                f"{'─' * 60}\n\n"
                f"Full logs:  {logs_cmd}\n"
                f"{recovery}\n"
                f"(Or re-run with --no-readyz-gate to accept this state.)"
            )
            if json_output:
                click.echo(json.dumps({
                    "ok": False,
                    "error": msg,
                    "app_name": app_name,
                    "app_url": app_url,
                    "readyz": checks,
                }, default=str))
                raise click.exceptions.Exit(1)
            raise click.ClickException(msg)

    # 6d. Register a UC version manifest for the deployed App (best-effort).
    # Runs after the App is live so a registration failure never blocks the
    # deploy. Skips with a loud notice when no UC name / model is configured.
    registered_version: str | None = None
    if register_uc:
        registered_version = _register_apps_manifest_step(
            module=module,
            config=_read_apx_agent_config(),
            app_name=app_name,
            bundle_target=bundle_target,
            uc_name_override=uc_name,
            extra_version_tags=extra_version_tags,
            profile=profile,
            log=log,
        )
    else:
        log("# --no-register-uc: skipping the UC version-manifest registration")

    # 7. Final report
    if json_output:
        # Enriched summary (issue #417): a CI job records what it shipped —
        # UC name + registered version, git/lock provenance (threaded from the
        # tags stamped on the manifest, never recomputed), and readyz checks.
        from ._apps_registry import GIT_DIRTY_TAG, GIT_SHA_TAG, LOCK_SHA256_TAG
        prov = extra_version_tags or {}
        dirty_tag = prov.get(GIT_DIRTY_TAG)
        click.echo(json.dumps({
            "ok": True,
            "app_name": app_name,
            "app_url": app_url,
            "bundle_target": bundle_target,
            "deploy_seconds": deploy_seconds,
            "run_seconds": run_seconds,
            "uc_name": _resolve_apps_uc_name(
                _read_apx_agent_config(), app_name, override=uc_name,
            ),
            "version": registered_version,
            "git_sha": prov.get(GIT_SHA_TAG),
            "git_dirty": None if dirty_tag is None else dirty_tag == "true",
            "lock_sha256": prov.get(LOCK_SHA256_TAG),
            # --env / --secret-env (issue #415): key NAMES only, never values.
            "env_keys": injected_env_keys,
            "secret_env_keys": injected_secret_env_keys,
            "readyz": readyz_checks,
        }, default=str))
    elif no_run:
        log(f"# app not started (--no-run); run `databricks bundle run "
            f"{bundle_key} --target {bundle_target}` or rerun without --no-run")
    else:
        log(f"# app ready: {app_name}")
        click.echo(app_url)


# ---------------------------------------------------------------------------
# publish-tools
# ---------------------------------------------------------------------------


@uc.command("publish")
@click.option("--module", default="agent:agent", help='Agent module spec.')
@click.option("--dry-run", is_flag=True, help="Report what would publish without writing.")
@click.option("--tools-table", default=None,
              help="UC Delta tools registry table. Defaults to [tool.apx.agent].tools_table or main.apx.agent_tools.")
@click.option("--no-registry", is_flag=True, help="Skip the tools registry write (UC publish only).")
@click.option("--profile", default=None, help="Databricks CLI profile.")
def publish_tools_cmd(
    module: str,
    dry_run: bool,
    tools_table: str | None,
    no_registry: bool,
    profile: str | None,
) -> None:
    """Publish all @tool(uc=...) decorated tools to Unity Catalog.

    Also registers each tool as a standalone entry in the org tools registry
    (``main.apx.agent_tools`` by default) so they are discoverable by any
    agent without being tied to a specific agent deployment.
    """
    from apx_agent import publish_tools_to_uc
    from apx_agent._publish import publish_standalone_tools_to_registry
    from apx_agent._resources import _iter_tool_fns
    from apx_agent._tool import get_tool_metadata

    cfg = _read_apx_agent_config()
    profile = profile or cfg.get("profile") or os.environ.get("DATABRICKS_CONFIG_PROFILE")
    if profile:
        os.environ["DATABRICKS_CONFIG_PROFILE"] = profile
    tools_table = tools_table or cfg.get("tools_table") or "main.apx.agent_tools"
    assert tools_table is not None

    agent = _load_finalized_agent(module)
    results = publish_tools_to_uc(agent, dry_run=dry_run)
    if not results:
        click.echo("No @tool(uc=...) decorated tools found in this agent.")
        click.echo("To publish a tool, decorate a function with @tool(uc='catalog.schema.fn_name'):")
        click.echo("  from apx_agent import tool")
        click.echo("  @tool(uc='main.tools.my_fn')")
        click.echo("  def my_fn(x: str) -> str: ...")
        return
    for r in results:
        prefix = "DRY-RUN" if r.skipped else "PUBLISHED"
        grants = ", ".join(r.grants_applied) if r.grants_applied else "none"
        click.echo(f"  {prefix}  {r.uc_name}  (grants: {grants})")

    # --- Write standalone rows to the tools registry ---
    if not no_registry and not dry_run:
        # Collect only the tool functions that were actually published to UC.
        published_names = {r.function_name for r in results if not r.skipped}
        tool_fns = [
            fn for fn in _iter_tool_fns(agent)
            if getattr(fn, "__name__", None) in published_names
        ]
        if tool_fns:
            try:
                ws, _ = _connect_workspace(profile)
                n = publish_standalone_tools_to_registry(
                    tool_fns=tool_fns,
                    tools_table=tools_table,
                    ws=ws,
                )
                click.echo(click.style(f"  ✓ Tools registry updated ({n} standalone tools → {tools_table})", fg="green"))
            except Exception as exc:
                click.echo(click.style(f"  ✗ Tools registry write failed: {exc}", fg="yellow"), err=True)


# ---------------------------------------------------------------------------
# create-supervisor  /  publish
# ---------------------------------------------------------------------------


def _do_create_supervisor(
    *, name: str, description: str, instructions: str | None, profile: str | None, save: bool,
) -> None:
    """Create a Mosaic Supervisor Agent. Shared by `supervisor create` and the
    deprecated `agents create-supervisor` alias."""
    from apx_agent import create_supervisor_agent

    cfg = _read_apx_agent_config()
    profile = profile or cfg.get("profile") or os.environ.get("DATABRICKS_CONFIG_PROFILE")
    if profile:
        os.environ["DATABRICKS_CONFIG_PROFILE"] = profile

    default_instructions = (
        f"You are {name}, an AI assistant that routes questions to the best available agent. "
        "Analyse the user's request and delegate to whichever sub-agent is most relevant. "
        "If no sub-agent fits, answer directly using your own knowledge."
    )
    instructions = instructions or default_instructions

    click.echo(f"\nCreating Supervisor Agent '{name}' …")
    try:
        result = create_supervisor_agent(
            display_name=name, description=description, instructions=instructions,
        )
    except ImportError as exc:
        raise click.ClickException(str(exc))
    except Exception as exc:
        raise click.ClickException(
            f"Failed to create supervisor: {exc}\n"
            "Make sure Agent Bricks is enabled in your workspace:\n"
            "  https://docs.databricks.com/aws/en/generative-ai/agent-bricks/multi-agent-supervisor"
        )

    supervisor_id = (
        getattr(result, "supervisor_agent_id", None)
        or getattr(result, "id", None)
        or (result.get("supervisor_agent_id") or result.get("id") if isinstance(result, dict) else None)
    )

    click.echo(click.style("\nSupervisor Agent created!", fg="green"))
    click.echo(f"  Name          : {name}")
    if supervisor_id:
        click.echo(f"  supervisor_id : {supervisor_id}")

    if save and supervisor_id:
        saved = _save_to_pyproject("supervisor_id", supervisor_id)
        if saved:
            click.echo("\nSaved to pyproject.toml. Any agent in this project can now run:")
            click.echo("  apx-agent agents advertise")
        else:
            click.echo("\nAdd this to your shared pyproject.toml so every agent can advertise with zero args:")
            click.echo("\n  [tool.apx.agent]")
            click.echo(f'  supervisor_id = "{supervisor_id}"')


def _do_advertise(
    *, endpoint: str | None, display_name: str | None, description: str | None,
    endpoint_url: str | None, endpoint_type: str, registry_table: str | None,
    tools_table: str | None, no_registry: bool, no_tools: bool, module: str | None,
    profile: str | None,
) -> _AdvertiseResult:
    """Write the agent → discovery registry + its tools → tools registry.

    Shared by `agents advertise` and the deprecated `agents publish` alias.
    Returns ``(ws, endpoint, description)`` so a caller (publish) can chain the
    supervisor step without re-resolving.
    """
    from apx_agent import publish_to_registry
    from apx_agent._publish import publish_tools_to_registry

    cfg = _read_apx_agent_config()
    profile = profile or cfg.get("profile") or os.environ.get("DATABRICKS_CONFIG_PROFILE")
    if profile:
        os.environ["DATABRICKS_CONFIG_PROFILE"] = profile

    endpoint = endpoint or cfg.get("name")
    display_name = display_name or cfg.get("name")
    description = description or cfg.get("description")
    registry_table = registry_table or cfg.get("registry_table") or "main.apx.agent_registry"
    tools_table = tools_table or cfg.get("tools_table") or "main.apx.agent_tools"

    if not endpoint:
        endpoint = click.prompt("Serving endpoint / app name")
    if not description:
        description = click.prompt("What does this agent do?")
    assert endpoint is not None and description is not None
    assert registry_table is not None and tools_table is not None

    ws, ws_cfg = _connect_workspace(profile)
    ws_host = (ws_cfg.host or "").rstrip("/")
    if not endpoint_url and ws_host:
        endpoint_url = (
            f"{ws_host}/apps/{endpoint}" if endpoint_type == "apps"
            else f"{ws_host}/serving-endpoints/{endpoint}"
        )

    import re as _re
    _host_short = (ws_host.replace("https://", "").split(".")[0]) if ws_host else ""
    _agent_id = _re.sub(r"[^a-zA-Z0-9_]+", "_", f"{endpoint}_{_host_short}").strip("_").lower() or "agent"

    click.echo(f"\nAdvertising '{display_name or endpoint}':")
    click.echo(f"  Endpoint       : {endpoint}")
    click.echo(f"  URL            : {endpoint_url or '(unknown)'}")
    click.echo(f"  Description    : {description}")
    click.echo(f"  Registry table : {registry_table}")
    click.echo(f"  Tools table    : {tools_table}")
    click.echo()

    if not no_registry:
        try:
            publish_to_registry(
                name=endpoint, description=description,
                display_name=display_name or endpoint, endpoint_url=endpoint_url,
                endpoint_type=endpoint_type, supervisor_agent_id=cfg.get("supervisor_id"),
                registry_table=registry_table, ws=ws,
            )
            click.echo(click.style(f"  ✓ Registry table updated ({registry_table})", fg="green"))
        except Exception as exc:
            click.echo(click.style(f"  ✗ Registry write failed: {exc}", fg="yellow"), err=True)
            click.echo("    To query the registry: SELECT * FROM " + registry_table, err=True)

    if not no_tools:
        _module = module or _detect_module_spec()
        if _module:
            try:
                from apx_agent._resources import _iter_tool_fns
                _agent = _load_finalized_agent(_module)
                _tool_fns = list(_iter_tool_fns(_agent))
                n = publish_tools_to_registry(
                    agent_id=_agent_id, agent_name=display_name or endpoint,
                    tool_fns=_tool_fns, tools_table=tools_table, ws=ws,
                )
                click.echo(click.style(f"  ✓ Tools table updated ({n} tools → {tools_table})", fg="green"))
            except Exception as exc:
                click.echo(click.style(f"  ✗ Tools registry write failed: {exc}", fg="yellow"), err=True)
        else:
            click.echo(click.style(
                "  ~ Tools registry skipped (no --module and no agent:agent default found)",
                fg="yellow",
            ))
    return _AdvertiseResult(ws, endpoint, description)


def _do_supervisor_add(
    *, ws: Any, supervisor_id: str, endpoint: str | None, description: str | None,
    display_name: str | None, tool_id: str | None, app: str | None = None,
) -> None:
    """Register a deployed agent (serving endpoint or app) as a Supervisor sub-agent.

    Shared by `supervisor add` and the deprecated `agents publish` alias.
    Exactly one of ``endpoint`` (Model Serving) or ``app`` (Databricks App)
    must be set — ``publish_to_supervisor`` enforces this.
    """
    from apx_agent import publish_to_supervisor

    target = app or endpoint
    try:
        result = publish_to_supervisor(
            supervisor_agent_id=supervisor_id, serving_endpoint=endpoint,
            app_name=app,
            description=description or "", display_name=display_name or target,
            tool_id=tool_id, ws=ws,
        )
        click.echo(click.style(f"  ✓ Registered with Supervisor Agent ({supervisor_id})", fg="green"))
        returned_tool_id = (
            getattr(result, "tool_id", None)
            or (result.get("tool_id") if isinstance(result, dict) else None)
        )
        if returned_tool_id:
            click.echo(f"    tool_id: {returned_tool_id}  (pin in pyproject.toml for idempotent re-add)")
    except ImportError as exc:
        click.echo(click.style(f"  ✗ Supervisor registration skipped: {exc}", fg="yellow"), err=True)
    except Exception as exc:
        click.echo(click.style(f"  ✗ Supervisor registration failed: {exc}", fg="yellow"), err=True)


@agents.command("advertise")
@click.option("--endpoint", default=None, help="Serving endpoint / app name. Defaults to [tool.apx.agent].name.")
@click.option("--description", default=None, help="Routing description. Defaults to [tool.apx.agent].description.")
@click.option("--display-name", default=None, help="Human-readable name. Defaults to [tool.apx.agent].name.")
@click.option("--endpoint-url", default=None, help="Full URL of the deployed agent (stored in registry table).")
@click.option("--endpoint-type", default="apps", type=click.Choice(["apps", "model-serving"]),
              help="Deployment type stored in registry. Default: apps.")
@click.option("--registry-table", default=None,
              help="UC Delta agent registry table. Defaults to [tool.apx.agent].registry_table or main.apx.agent_registry.")
@click.option("--tools-table", default=None,
              help="UC Delta tools table. Defaults to [tool.apx.agent].tools_table or main.apx.agent_tools.")
@click.option("--no-registry", is_flag=True, help="Skip the agent registry write.")
@click.option("--no-tools", is_flag=True, help="Skip the tools registry write.")
@click.option("--module", default=None, help="Agent module spec (e.g. agent:agent). Used to enumerate tools.")
@click.option("--profile", default=None, help="Databricks CLI profile.")
def advertise(
    endpoint: str | None, description: str | None, display_name: str | None,
    endpoint_url: str | None, endpoint_type: str, registry_table: str | None,
    tools_table: str | None, no_registry: bool, no_tools: bool, module: str | None,
    profile: str | None,
) -> None:
    """Advertise this agent to the org's discovery registry (SQL-queryable).

    Writes the agent to the Delta agent registry and catalogs its tools in the
    tools table so other agents/people can find them. This is discovery only —
    to wire the agent into a Mosaic Supervisor for routing, use
    ``apx-agent supervisor add``.

    \b
      apx-agent deploy       # deploy to your workspace
      apx-agent advertise    # make it discoverable
    """
    _do_advertise(
        endpoint=endpoint, display_name=display_name, description=description,
        endpoint_url=endpoint_url, endpoint_type=endpoint_type,
        registry_table=registry_table, tools_table=tools_table,
        no_registry=no_registry, no_tools=no_tools, module=module, profile=profile,
    )


@main.group(cls=_ApxGroup)
def supervisor() -> None:
    """Mosaic AI Supervisor Agent — create one, and add deployed agents to it."""


@supervisor.command("create")
@click.option("--name", required=True, help="Display name for the supervisor (e.g. 'Acme AI Assistant').")
@click.option("--description", default="", help="Short description shown in the Databricks UI.")
@click.option("--instructions", default=None,
              help="System prompt controlling how the supervisor routes queries. "
                   "Defaults to a sensible 'route to the best sub-agent' prompt.")
@click.option("--profile", default=None, help="Databricks CLI profile.")
@click.option("--save/--no-save", default=True,
              help="Write supervisor_id to pyproject.toml after creation (default: yes).")
def supervisor_create(
    name: str, description: str, instructions: str | None, profile: str | None, save: bool,
) -> None:
    """Create a Mosaic AI Supervisor Agent for your org's A2A registry.

    Run once per workspace. The supervisor routes user queries across all agents
    added with ``apx-agent supervisor add``. Requires Agent Bricks (Beta).

    \b
      apx-agent supervisor create --name "Acme AI Assistant"
    """
    _do_create_supervisor(
        name=name, description=description, instructions=instructions,
        profile=profile, save=save,
    )


@supervisor.command("add")
@click.option("--endpoint", default=None,
              help="Model Serving endpoint name. Defaults to [tool.apx.agent].name. "
                   "Mutually exclusive with --app.")
@click.option("--app", default=None,
              help="Databricks App name hosting the agent (apps-deployed apx agents). "
                   "Also accepts a UC model name (catalog.schema.model) — resolved via "
                   "its apx.apps.app_name tag. Mutually exclusive with --endpoint. "
                   "Requires databricks-sdk >= 0.120.")
@click.option("--supervisor", "supervisor_id", default=None,
              help="Supervisor Agent ID. Reads [tool.apx.agent].supervisor_id if set.")
@click.option("--description", default=None, help="Routing description. Defaults to [tool.apx.agent].description.")
@click.option("--display-name", default=None, help="Human-readable name. Defaults to [tool.apx.agent].name.")
@click.option("--tool-id", default=None, help="Stable tool_id for idempotent re-add.")
@click.option("--profile", default=None, help="Databricks CLI profile.")
def supervisor_add(
    endpoint: str | None, app: str | None, supervisor_id: str | None,
    description: str | None, display_name: str | None, tool_id: str | None,
    profile: str | None,
) -> None:
    """Register a deployed agent as a sub-agent on a Mosaic Supervisor.

    The supervisor then routes relevant user queries to this agent. Deploy and
    (optionally) ``advertise`` the agent first. Serving-endpoint agents register
    with --endpoint; apps-deployed agents with --app.

    \b
      apx-agent supervisor add --supervisor <id> --endpoint my-agent
      apx-agent supervisor add --supervisor <id> --app my-agent-app
    """
    if endpoint and app:
        raise click.UsageError("--endpoint and --app are mutually exclusive — pass one.")
    cfg = _read_apx_agent_config()
    profile = profile or cfg.get("profile") or os.environ.get("DATABRICKS_CONFIG_PROFILE")
    if profile:
        os.environ["DATABRICKS_CONFIG_PROFILE"] = profile
    if not app:
        endpoint = endpoint or cfg.get("name")
    display_name = display_name or cfg.get("name")
    description = description or cfg.get("description")
    supervisor_id = supervisor_id or cfg.get("supervisor_id")
    if not supervisor_id:
        raise click.UsageError(
            "--supervisor is required (or set [tool.apx.agent].supervisor_id). "
            "Create one with `apx-agent supervisor create`."
        )
    if not endpoint and not app:
        endpoint = click.prompt("Serving endpoint name")
    ws, _ = _connect_workspace(profile)
    if app and "." in app:
        # A UC model name — resolve the App it deployed to via the same tag
        # `agents delete`/`status` use (apx.apps.app_name).
        uc_name = app
        try:
            model = ws.registered_models.get(uc_name)
        except Exception as exc:
            raise click.ClickException(
                f"Could not read UC model {uc_name!r} to resolve its app name: {exc}"
            ) from exc
        uc_tags = {t.key: t.value for t in (getattr(model, "tags", None) or [])}
        app = uc_tags.get("apx.apps.app_name")
        if not app:
            raise click.ClickException(
                f"UC model {uc_name!r} has no apx.apps.app_name tag — pass the "
                "Databricks App name directly with --app."
            )
    click.echo(f"\nAdding '{display_name or app or endpoint}' to Supervisor {supervisor_id}:")
    _do_supervisor_add(
        ws=ws, supervisor_id=supervisor_id, endpoint=endpoint, app=app,
        description=description, display_name=display_name, tool_id=tool_id,
    )


@agents.command("create-supervisor")
@click.option("--name", required=True, help="Display name for the supervisor (e.g. 'Acme AI Assistant').")
@click.option("--description", default="", help="Short description shown in the Databricks UI.")
@click.option(
    "--instructions", default=None,
    help="System prompt controlling how the supervisor routes queries. "
         "Defaults to a sensible 'route to the best sub-agent' prompt.",
)
@click.option("--profile", default=None, help="Databricks CLI profile.")
@click.option("--save/--no-save", default=True,
              help="Write supervisor_id to pyproject.toml after creation (default: yes).")
def create_supervisor(
    name: str,
    description: str,
    instructions: str | None,
    profile: str | None,
    save: bool,
) -> None:
    """Create a Mosaic AI Supervisor Agent for your org's A2A registry.

    Run this once per workspace. The supervisor routes user queries across all
    agents published with ``apx-agent publish``. After creation, pin the
    printed supervisor_id in your org's shared pyproject.toml so every agent
    can publish with a single zero-argument command.

    DEPRECATED — use ``apx-agent supervisor create``. This alias still works.
    """
    click.echo(
        "# `agents create-supervisor` is deprecated; use `apx-agent supervisor create`.",
        err=True,
    )
    _do_create_supervisor(
        name=name, description=description, instructions=instructions,
        profile=profile, save=save,
    )


@agents.command("publish")
@click.option("--endpoint", default=None, help="Serving endpoint / app name. Defaults to [tool.apx.agent].name.")
@click.option("--supervisor", "supervisor_id", default=None,
              help="Supervisor Agent ID. Reads [tool.apx.agent].supervisor_id if set; skipped when absent.")
@click.option("--description", default=None,
              help="Routing description. Defaults to [tool.apx.agent].description.")
@click.option("--display-name", default=None, help="Human-readable name. Defaults to [tool.apx.agent].name.")
@click.option("--tool-id", default=None, help="Stable tool_id for idempotent Supervisor re-publish.")
@click.option("--endpoint-url", default=None, help="Full URL of the deployed agent (stored in registry table).")
@click.option("--endpoint-type", default="apps", type=click.Choice(["apps", "model-serving"]),
              help="Deployment type stored in registry. Default: apps.")
@click.option("--registry-table", default=None,
              help="UC Delta registry table. Defaults to [tool.apx.agent].registry_table or main.apx.agent_registry.")
@click.option("--tools-table", default=None,
              help="UC Delta tools table. Defaults to [tool.apx.agent].tools_table or main.apx.agent_tools.")
@click.option("--no-registry", is_flag=True, help="Skip the Delta registry write.")
@click.option("--no-tools", is_flag=True, help="Skip the tools registry write.")
@click.option("--module", default=None, help="Agent module spec (e.g. agent:agent). Used to enumerate tools for the tools registry.")
@click.option("--profile", default=None, help="Databricks CLI profile.")
def publish(
    endpoint: str | None,
    supervisor_id: str | None,
    description: str | None,
    display_name: str | None,
    tool_id: str | None,
    endpoint_url: str | None,
    endpoint_type: str,
    registry_table: str | None,
    tools_table: str | None,
    no_registry: bool,
    no_tools: bool,
    module: str | None,
    profile: str | None,
) -> None:
    """Advertise this agent + (when a supervisor is configured) wire it in.

    DEPRECATED — this kitchen-sink command is split by intent:
      * ``apx-agent agents advertise``  — discovery registry (agent + tools)
      * ``apx-agent supervisor add``    — wire into a Mosaic Supervisor

    This alias still works: it runs ``advertise`` and, when ``supervisor_id`` is
    configured, ``supervisor add`` — preserving the one-shot behavior.
    """
    click.echo(
        "# `agents publish` is deprecated; use `apx-agent agents advertise` "
        "(discovery) and `apx-agent supervisor add` (routing). Running both now.",
        err=True,
    )
    cfg = _read_apx_agent_config()
    supervisor_id = supervisor_id or cfg.get("supervisor_id")

    ws, resolved_endpoint, resolved_description = _do_advertise(
        endpoint=endpoint, display_name=display_name, description=description,
        endpoint_url=endpoint_url, endpoint_type=endpoint_type,
        registry_table=registry_table, tools_table=tools_table,
        no_registry=no_registry, no_tools=no_tools, module=module, profile=profile,
    )
    if supervisor_id:
        # Apps-deployed agents register as an "app" supervisor tool; a
        # serving-endpoint tool pointing at an app name would be broken.
        is_apps = endpoint_type == "apps"
        _do_supervisor_add(
            ws=ws, supervisor_id=supervisor_id,
            endpoint=None if is_apps else resolved_endpoint,
            app=resolved_endpoint if is_apps else None,
            description=resolved_description, display_name=display_name,
            tool_id=tool_id,
        )
    else:
        click.echo("  Supervisor A2A : (not configured — skipping; "
                   "run `apx-agent supervisor create` to enable A2A routing)")
    click.echo()


# ---------------------------------------------------------------------------
# mcp-config
# ---------------------------------------------------------------------------


@uc.command("mcp-config")
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


@agents.command()
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
    ws, _ = _connect_workspace(profile)

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
# describe (was: info)
# ---------------------------------------------------------------------------


def _find_apx_specs(cwd: Path) -> list[Path]:
    """YAML files in *cwd* that load as a valid apx spec (best-effort)."""
    from ._yaml_spec import load_spec

    found = []
    for p in sorted([*cwd.glob("*.yaml"), *cwd.glob("*.yml")]):
        try:
            load_spec(p)
            found.append(p)
        except Exception:
            continue
    return found


def _describe_from_spec(yaml_path: Path, fmt: str) -> None:
    """Render what a YAML spec declares — pure-local, no agent resolution."""
    from ._yaml_spec import SpecValidationError, load_spec

    try:
        cfg = load_spec(yaml_path)
    except SpecValidationError as e:
        raise click.ClickException(f"Invalid spec {yaml_path}: {e}") from e

    payload = {
        "spec": str(yaml_path),
        "name": cfg.name,
        "model": cfg.model,
        "instructions": cfg.instructions,
        "template": cfg.template,
        "tools": cfg.tools,
        "sub_agents": cfg.sub_agents,
    }
    if fmt == "json":
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"Agent spec: {yaml_path.name}")
    click.echo(f"  name:  {cfg.name}")
    click.echo(f"  model: {cfg.model}")
    if cfg.template:
        loc = f"{cfg.template.get('catalog')}.{cfg.template.get('schema')}"
        click.echo(f"  template: {cfg.template.get('name')} ({loc})")
    click.echo("\nInstructions:")
    if cfg.instructions.strip():
        click.echo(f"  {cfg.instructions}")
    else:
        click.echo("  (empty — add 'instructions:' to the YAML to say what it does)")
    click.echo(f"\nDeclared tools ({len(cfg.tools)}):")
    if not cfg.tools:
        click.echo("  (none)")
    for t in cfg.tools:
        click.echo(f"  - {t.get('type', '?')}: {t.get('name', '')}".rstrip())
    if cfg.sub_agents:
        click.echo(f"\nSub-agents ({len(cfg.sub_agents)}):")
        for s in cfg.sub_agents:
            click.echo(f"  - {s}")


def _pick_local_spec(specs: "list[Path]") -> Path:
    """Interactive picklist of local ``.yaml`` agent specs → the chosen path.

    Used when ``agents describe`` finds more than one spec in the project. In a
    terminal it prompts (same style as the deployed-agent picklist);
    non-interactively it lists the specs and exits with guidance."""
    specs = sorted(specs, key=lambda p: p.name)
    if not sys.stdin.isatty():
        listing = "\n".join(f"  {p.name}" for p in specs)
        raise click.ClickException(
            "Multiple specs here — pass one:\n"
            f"  apx-agent agents describe <spec>.yaml\n\n{listing}"
        )
    click.secho("\nLocal agent specs:", fg="cyan", bold=True)
    click.echo()
    for i, p in enumerate(specs, 1):
        num = click.style(f"  {i:2})", fg="bright_green", bold=True)
        click.echo(f"{num} {click.style(p.name, fg='cyan')}")
    click.echo()
    idx = click.prompt(
        click.style("Spec number", fg="cyan", bold=True),
        type=click.IntRange(1, len(specs)),
        default=1,
    )
    return specs[idx - 1]


def _pick_app_agent(profile: str | None) -> str:
    """Interactive picklist of deployed app agents → the chosen app name.

    Used when ``agents describe --app`` is run without a name. In a terminal it
    discovers the workspace's running apx agents and prompts; non-interactively
    it lists them and exits with guidance (never hangs)."""
    from ._apps_discovery import discover_app_agents

    ws = _require_sdk(profile)
    click.secho("(discovering deployed agents…)", fg="bright_black", err=True)
    agents_ = sorted(discover_app_agents(ws), key=lambda a: a.app_name)
    if not agents_:
        raise click.ClickException(
            "No deployed app agents found. Run `apx-agent agents list` to check, "
            "or deploy one with `apx-agent agents deploy`."
        )
    if not sys.stdin.isatty():
        listing = "\n".join(f"  {a.app_name}" for a in agents_)
        raise click.ClickException(
            "Multiple deployed agents — pass one:\n"
            f"  apx-agent agents describe <name> --app\n\n{listing}"
        )
    click.secho("\nDeployed agents:", fg="cyan", bold=True)
    click.echo()
    for i, a in enumerate(agents_, 1):
        num = click.style(f"  {i:2})", fg="bright_green", bold=True)
        tools = f"{a.tool_count} tool{'' if a.tool_count == 1 else 's'}"
        click.echo(f"{num} {click.style(a.name, fg='cyan')}  "
                   f"{click.style(tools, fg='bright_black')}")
    click.echo()
    idx = click.prompt(
        click.style("Agent number", fg="cyan", bold=True),
        type=click.IntRange(1, len(agents_)),
        default=1,
    )
    return agents_[idx - 1].app_name


def _describe_app_agent(name: str, profile: str | None, fmt: str) -> None:
    """Fetch and print a deployed Databricks App agent's live A2A card."""
    from ._apps_discovery import fetch_app_card

    ws = _require_sdk(profile)
    hit = fetch_app_card(ws, name)
    if hit is None:
        raise click.ClickException(
            f"No running app agent named {name!r} found. "
            "Run `apx-agent agents list` to see what's deployed."
        )
    card = hit["card"]
    if fmt == "json":
        click.echo(json.dumps(hit, indent=2, default=str))
        return

    skills = card.get("skills") or []
    click.echo(f"{card.get('name') or name}   (app: {hit['app_name']})")
    if card.get("description"):
        click.echo(card["description"])
    click.echo(f"URL: {hit['url']}")
    caps = card.get("capabilities") or {}
    flags = [k for k in ("streaming", "multiTurn") if caps.get(k)]
    if flags:
        click.echo(f"Capabilities: {', '.join(flags)}")
    click.echo(f"\nTools ({len(skills)}):")
    if not skills:
        click.echo("  (none advertised)")
    for s in skills:
        sid = s.get("name") or s.get("id") or "?"
        desc = (s.get("description") or "").strip().splitlines()
        params = list((s.get("inputSchema") or {}).get("properties", {}).keys())
        line = f"  {sid:<24}  {desc[0] if desc else ''}"
        click.echo(line.rstrip())
        if params:
            click.echo(f"  {'':<24}  params: {', '.join(params)}")


@agents.command("describe")
@click.argument("spec", required=False, default=None, metavar="[SPEC]")
@click.option("--module", default="agent:agent", help='Agent module spec.')
@click.option(
    "--format", "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format.",
)
@click.option("--app", "app", is_flag=True,
              help="SPEC is a deployed Databricks App name; fetch its live A2A card.")
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks config profile (~/.databrickscfg). Used with --app.")
def info(spec: str | None, module: str, fmt: str, app: bool, profile: str | None) -> None:
    """Introspect an agent — tools, resources, sub-agents, instructions.

    SPEC may be a .yaml spec (shows what it declares). Omit it inside a
    scaffolded project to introspect the materialized agent, or when only a
    lone spec is present it's auto-detected. Omit it OUTSIDE a project to pick
    from the workspace's deployed agents (the ``--app`` picklist, no flag
    needed). A BARE name (e.g. ``payroll-coworker``) is taken as a deployed
    Databricks App — no ``--app`` needed; its live A2A card is fetched.

    With ``--app``, SPEC is the name of a deployed Databricks App: its live A2A
    card is fetched and its tools printed (see ``apx-agent agents list``). Run
    ``--app`` WITHOUT a name to pick from a list of deployed agents.

    Pure local — no Databricks calls — unless SPEC is a deployed-app name or
    ``--app`` is given.
    """
    if app:
        if not spec:
            spec = _pick_app_agent(profile)  # picklist of deployed agents
        _describe_app_agent(spec, profile, fmt)
        return

    # Auto-detect: a BARE name (not a .yaml/.yml spec, not a `module:attr`
    # path) is never a local describe target — the local path below only
    # resolves .yaml specs and module:attr. So treat it as a deployed app name,
    # making `agents describe payroll-coworker` just work without --app.
    if spec and not spec.endswith((".yaml", ".yml")) and ":" not in spec:
        _describe_app_agent(spec, profile, fmt)
        return

    from apx_agent._resources import (
        _iter_sub_agents,
        _iter_tool_fns,
        collect_resource_specs,
        get_resources,
    )
    from apx_agent._tool import get_tool_metadata

    # A .yaml SPEC (explicit, or passed via --module) → describe the spec.
    spec_arg = spec if (spec and spec.endswith((".yaml", ".yml"))) else None
    if spec_arg is None and module.endswith((".yaml", ".yml")):
        spec_arg = module
    if spec_arg is not None:
        spec_path = Path(spec_arg)
        if not spec_path.exists():
            raise click.ClickException(f"Spec file not found: {spec_arg}")
        _describe_from_spec(spec_path, fmt)
        return

    # No explicit module/spec: prefer a materialized project, else a lone spec,
    # else fall back to the DEPLOYED picklist (so a bare `agents describe`
    # outside a project just lets you pick a deployed agent — no --app needed).
    if module == "agent:agent":
        detected = _detect_module_spec()
        if detected is not None:
            module = detected
        else:
            specs = _find_apx_specs(Path.cwd())
            if len(specs) == 1:
                _describe_from_spec(specs[0], fmt)
                return
            if len(specs) > 1:
                _describe_from_spec(_pick_local_spec(specs), fmt)
                return
            # Nothing local here → pick from the workspace's deployed agents.
            _describe_app_agent(_pick_app_agent(profile), profile, fmt)
            return

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
    if not tools_info:
        click.echo("  (none — add tools with @tool, uc_function_tool(), or genie_tool())")
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
# traces list — inspect MLflow traces
# ---------------------------------------------------------------------------


@traces.command("list")
@click.option("--experiment", default=None,
              help="MLflow experiment name/id. Falls back to "
                   "[tool.apx.agent].experiment in pyproject.toml.")
@click.option("--agent", "agent_name", default=None,
              help="Filter to traces where apx.agent.name matches.")
@click.option("--operation", default=None,
              help="Filter by apx.operation (predict, tool_call, model_call, etc.).")
@click.option("--user", "user_filter", default=None,
              help="Filter to traces from this user (matches apx.user or mlflow.user tag).")
@click.option("--min-latency", "min_latency_ms", default=None, type=int,
              help="Only show traces with duration >= MIN_LATENCY ms.")
@click.option("--error-only", is_flag=True, default=False,
              help="Only show traces with ERROR or FAILED status.")
@click.option("--tag", "tag_filters", multiple=True, metavar="KEY=VAL",
              help="Filter by an attribute tag value — KEY=VAL (repeatable).")
@click.option("--limit", default=20, type=int, help="Max traces to return.")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def trace(
    experiment: str | None,
    agent_name: str | None,
    operation: str | None,
    user_filter: str | None,
    min_latency_ms: int | None,
    error_only: bool,
    tag_filters: tuple[str, ...],
    limit: int,
    fmt: str,
) -> None:
    """Fetch recent MLflow traces for a deployed agent.

    Use --user, --min-latency, --error-only, and --tag to narrow results.
    All filters combine with AND semantics.
    """
    try:
        import mlflow
    except ImportError as e:
        raise click.ClickException(
            "apx-agent trace requires mlflow. Install with: uv add 'apx-agent[eval]'"
        ) from e

    effective_experiment = experiment or _read_apx_agent_config().get("experiment")
    if not effective_experiment:
        raise click.UsageError(
            "Pass --experiment NAME or set [tool.apx.agent].experiment in pyproject.toml."
        )

    # Parse --tag KEY=VAL filters now; validate before hitting the API.
    parsed_tag_filters: list[tuple[str, str]] = []
    for kv in tag_filters:
        if "=" not in kv:
            raise click.UsageError(f"--tag value must be KEY=VAL, got: {kv!r}")
        k, v = kv.split("=", 1)
        parsed_tag_filters.append((k.strip(), v.strip()))

    filter_parts: list[str] = []
    if agent_name:
        filter_parts.append(f"attributes.`apx.agent.name` = '{agent_name}'")
    if operation:
        filter_parts.append(f"attributes.`apx.operation` = '{operation}'")
    filter_string = " AND ".join(filter_parts) if filter_parts else None

    # Fetch more than requested when post-filters are active so we can still
    # return --limit rows after they trim the result set.
    fetch_limit = limit * 4 if (user_filter or min_latency_ms or error_only or parsed_tag_filters) else limit

    from ._mlflow_tracing import search_traces_for_experiment

    try:
        raw = search_traces_for_experiment(
            effective_experiment,
            filter_string=filter_string,
            max_results=fetch_limit,
        )
    except Exception as e:
        raise click.ClickException(f"mlflow.search_traces failed: {e}") from e

    rows = _normalise_trace_rows(raw)

    # Post-filters (applied client-side for broad MLflow version compat).
    if error_only:
        rows = [r for r in rows if (r.get("status") or "").upper() in ("ERROR", "FAILED")]
    if user_filter:
        rows = [r for r in rows if (r.get("user") or "") == user_filter]
    if min_latency_ms is not None:
        rows = [r for r in rows if (r.get("duration_ms") or 0) >= min_latency_ms]
    for tag_key, tag_val in parsed_tag_filters:
        rows = [r for r in rows if str((r.get("tags") or {}).get(tag_key, "")) == tag_val]
    rows = rows[:limit]

    if fmt == "json":
        click.echo(json.dumps(rows, indent=2, default=str))
        return

    if not rows:
        click.echo("No traces matched.")
        click.echo("Possible causes:")
        click.echo("  • The agent hasn't handled any requests yet — send a message via the Chat UI or API.")
        click.echo("  • Tracing isn't enabled — set MLFLOW_TRACKING_URI or configure an experiment.")
        click.echo("  • Wrong experiment name — check [tool.apx.agent].experiment in pyproject.toml.")
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
    Includes ``user`` (from apx.user / mlflow.user tags) and ``tags`` for
    client-side post-filtering in ``traces list``.
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
                "user": attrs.get("apx.user") or attrs.get("mlflow.user"),
                "status": rec.get("status"),
                "duration_ms": rec.get("execution_time_ms"),
                "tags": attrs,
            })
        return rows
    # List-of-Trace case
    for t in traces or []:
        info = getattr(t, "info", None)
        data = getattr(t, "data", None)
        spans = getattr(data, "spans", None) or []
        # Pull root-span attributes if available; otherwise fall back to
        # trace-level tags.
        root_attrs: dict[str, Any] = {}
        if spans:
            root = spans[0]
            root_attrs = dict(getattr(root, "attributes", {}) or {})
        tags = dict(getattr(info, "tags", {}) or {}) if info else {}
        merged_attrs = {**tags, **root_attrs}
        rows.append({
            "trace_id": getattr(info, "trace_id", None) or getattr(info, "request_id", None),
            "agent_name": merged_attrs.get("apx.agent.name"),
            "operation": merged_attrs.get("apx.operation"),
            "user": merged_attrs.get("apx.user") or merged_attrs.get("mlflow.user"),
            "status": getattr(info, "status", None),
            "duration_ms": getattr(info, "execution_time_ms", None),
            "tags": merged_attrs,
        })
    return rows


# ---------------------------------------------------------------------------
# traces get — drill into a single trace by ID
# ---------------------------------------------------------------------------


@traces.command("get")
@click.argument("trace_id")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def traces_get_cmd(trace_id: str, fmt: str) -> None:
    """Show details and the span tree for a single trace by TRACE_ID."""
    try:
        import mlflow
    except ImportError as e:
        raise click.ClickException(
            "apx-agent traces get requires mlflow. Install with: uv add 'apx-agent[eval]'"
        ) from e

    try:
        trace = mlflow.get_trace(trace_id, silent=True)
    except Exception as e:
        raise click.ClickException(f"mlflow.get_trace failed: {e}") from e

    if trace is None:
        raise click.ClickException(
            f"Trace {trace_id!r} not found. "
            "Check the ID with:  apx-agent traces list"
        )

    info = getattr(trace, "info", None)
    data = getattr(trace, "data", None)
    spans = getattr(data, "spans", None) or []
    tags = dict(getattr(info, "tags", {}) or {}) if info else {}

    if fmt == "json":
        span_dicts = []
        for sp in spans:
            span_dicts.append({
                "name": getattr(sp, "name", None),
                "span_id": getattr(sp, "span_id", None),
                "parent_span_id": getattr(sp, "parent_span_id", None),
                "start_time_ns": getattr(sp, "start_time_ns", None),
                "end_time_ns": getattr(sp, "end_time_ns", None),
                "status": str(getattr(sp, "status", None)),
                "attributes": dict(getattr(sp, "attributes", {}) or {}),
            })
        click.echo(json.dumps({
            "trace_id": getattr(info, "trace_id", None) or getattr(info, "request_id", None),
            "status": str(getattr(info, "status", None)),
            "duration_ms": getattr(info, "execution_time_ms", None),
            "tags": tags,
            "spans": span_dicts,
        }, indent=2, default=str))
        return

    tid = getattr(info, "trace_id", None) or getattr(info, "request_id", trace_id)
    status = str(getattr(info, "status", "-"))
    duration = getattr(info, "execution_time_ms", None)
    duration_str = f"{duration} ms" if duration is not None else "-"
    click.echo(f"trace:    {tid}")
    click.echo(f"status:   {status}")
    click.echo(f"duration: {duration_str}")
    if tags:
        click.echo("tags:")
        for k, v in sorted(tags.items()):
            click.echo(f"  {k}: {v}")

    if not spans:
        click.echo("(no spans recorded)")
        return

    # Build parent→children index for tree rendering.
    children: dict[str | None, list[Any]] = {}
    for sp in spans:
        pid = getattr(sp, "parent_span_id", None)
        children.setdefault(pid, []).append(sp)

    click.echo("\nspans:")

    def _print_span(sp: Any, indent: int) -> None:
        name = getattr(sp, "name", "?")
        sid = getattr(sp, "span_id", "?")
        sp_status = str(getattr(sp, "status", "?"))
        start_ns = getattr(sp, "start_time_ns", None)
        end_ns = getattr(sp, "end_time_ns", None)
        span_ms = int((end_ns - start_ns) / 1_000_000) if (start_ns and end_ns) else None
        span_ms_str = f"  [{span_ms} ms]" if span_ms is not None else ""
        prefix = "  " * indent
        click.echo(f"{prefix}├─ {name}  ({sp_status}){span_ms_str}  id={sid}")
        for child in children.get(sid, []):
            _print_span(child, indent + 1)

    # Print top-level spans (those with no parent or whose parent is unknown).
    known_ids = {getattr(sp, "span_id", None) for sp in spans}
    roots = [
        sp for sp in spans
        if getattr(sp, "parent_span_id", None) not in known_ids
    ]
    for root in roots:
        _print_span(root, 0)


# ---------------------------------------------------------------------------
# traces delete — purge traces
# ---------------------------------------------------------------------------


@traces.command("delete")
@click.option("--trace-id", "trace_ids", multiple=True,
              help="Specific trace IDs to delete (repeatable).")
@click.option("--experiment", default=None,
              help="MLflow experiment name. Falls back to [tool.apx.agent].experiment. "
                   "Required when using --hours instead of --trace-id.")
@click.option("--hours", default=None, type=int,
              help="Delete traces older than HOURS hours. "
                   "Mutually exclusive with --trace-id.")
@click.option("--max-traces", default=None, type=int,
              help="Cap the number of traces deleted when using --hours.")
@click.option("--yes", "-y", "assume_yes", is_flag=True,
              help="Skip confirmation prompt.")
def traces_delete_cmd(
    trace_ids: tuple[str, ...],
    experiment: str | None,
    hours: int | None,
    max_traces: int | None,
    assume_yes: bool,
) -> None:
    """Delete traces from the MLflow store.

    Two modes:

    \b
      By ID:    apx-agent traces delete --trace-id abc123 --trace-id def456
      By age:   apx-agent traces delete --experiment /exp/my-agent --hours 48
    """
    try:
        import mlflow
    except ImportError as e:
        raise click.ClickException(
            "apx-agent traces delete requires mlflow. Install with: uv add 'apx-agent[eval]'"
        ) from e

    if trace_ids and hours is not None:
        raise click.UsageError("--trace-id and --hours are mutually exclusive.")
    if not trace_ids and hours is None:
        raise click.UsageError("Pass --trace-id ID [...] or --hours N to select traces.")

    if trace_ids:
        if not assume_yes:
            click.confirm(
                f"Delete {len(trace_ids)} trace(s)? This cannot be undone.", abort=True
            )
        client = mlflow.MlflowClient()
        # delete_traces requires experiment_id; look it up from any experiment
        # associated with these traces by fetching each one.
        exp_ids: set[str] = set()
        for tid in trace_ids:
            t = mlflow.get_trace(tid, silent=True)
            if t is None:
                click.echo(f"  warning: trace {tid!r} not found, skipping.", err=True)
                continue
            info = getattr(t, "info", None)
            eid = getattr(info, "experiment_id", None)
            if eid:
                exp_ids.add(eid)
        if not exp_ids:
            raise click.ClickException("Could not resolve experiment IDs for the given trace IDs.")
        deleted = 0
        for eid in exp_ids:
            deleted += client.delete_traces(
                experiment_id=eid,
                trace_ids=list(trace_ids),
            )
        click.echo(f"Deleted {deleted} trace(s).")
        return

    # --hours mode
    effective_experiment = experiment or _read_apx_agent_config().get("experiment")
    if not effective_experiment:
        raise click.UsageError(
            "Pass --experiment NAME or set [tool.apx.agent].experiment in pyproject.toml."
        )
    import time as _time
    assert hours is not None
    cutoff_ms = int((_time.time() - hours * 3600) * 1000)
    if not assume_yes:
        click.confirm(
            f"Delete traces in {effective_experiment!r} older than {hours}h? "
            "This cannot be undone.",
            abort=True,
        )
    try:
        exp = mlflow.get_experiment_by_name(effective_experiment)
    except Exception as e:
        raise click.ClickException(f"Could not find experiment {effective_experiment!r}: {e}") from e
    if exp is None:
        raise click.ClickException(f"Experiment {effective_experiment!r} not found.")
    client = mlflow.MlflowClient()
    kwargs: dict[str, Any] = {"experiment_id": exp.experiment_id, "max_timestamp_millis": cutoff_ms}
    if max_traces is not None:
        kwargs["max_traces"] = max_traces
    deleted = client.delete_traces(**kwargs)
    click.echo(f"Deleted {deleted} trace(s).")


# ---------------------------------------------------------------------------
# eval lint — static checks
# ---------------------------------------------------------------------------


@eval_group.command("lint")
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


@agents.command("hot-swap")
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
@click.option("--json", "as_json", is_flag=True,
              help="Emit a single JSON result object on stdout "
                   "(ok:false + error on failure).")
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
    as_json: bool,
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
    with _json_cli_errors(as_json):
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
                as_json=as_json,
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
            as_json=as_json,
        )


def _hot_swap_model_serving(
    *, endpoint: str, model: str, no_wait: bool, as_json: bool = False,
) -> None:
    """Click handler body for the Model Serving hot-swap path."""
    from ._hot_swap import hot_swap_model

    try:
        result = hot_swap_model(endpoint, model, wait=not no_wait)
    except Exception as e:
        raise click.ClickException(f"hot-swap failed: {type(e).__name__}: {e}") from e

    if as_json:
        click.echo(json.dumps({
            "ok": True,
            "target": "model-serving",
            "endpoint": result.endpoint_name,
            "new_model": result.new_model,
            "previous_model": result.previous_model,
            "served_entities_updated": result.served_entities_updated,
        }))
        return
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
    as_json: bool = False,
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
        raise click.ClickException(f"hot-swap --target apps failed: {type(e).__name__}: {e}") from e

    if as_json:
        click.echo(json.dumps({
            "ok": True,
            "target": "apps",
            "app_name": result.app_name,
            "bundle_target": result.bundle_target,
            "var_name": result.var_name,
            "new_value": result.new_value,
            "previous_value": result.previous_value,
        }))
        return
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
# eval test — local smoke test
# ---------------------------------------------------------------------------


@eval_group.command("test")
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
            "Install with: uv add 'apx-agent[eval]'"
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


# (list group removed — use `apx-agent agents list` or `apx-agent uc catalogs/schemas/tables/tools`)


def _connect_workspace(profile: str | None) -> "tuple[Any, Any]":
    """Return (WorkspaceClient, Config), with an interactive profile picker on auth errors.

    When no profile is given and auth is ambiguous (e.g. multiple profiles for
    the same host), prompts the user to pick one interactively. Falls back to a
    plain error list when stdin is not a TTY (CI / piped usage).
    """
    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.config import Config
        cfg = Config(profile=profile) if profile else Config()
        return WorkspaceClient(config=cfg), cfg
    except ImportError as e:
        raise click.ClickException(
            "databricks-sdk is required. Install with: uv add databricks-sdk"
        ) from e
    except Exception as e:
        if profile is not None:
            raise click.ClickException(
                f"Could not connect to Databricks with profile '{profile}': {e}"
            ) from e
        # No profile given — offer an interactive picker when running in a terminal.
        profiles = _list_databricks_profiles()
        if not profiles:
            raise click.ClickException(
                f"Could not resolve Databricks authentication: {e}\n"
                "Run: databricks auth login --host <workspace-url>"
            ) from e
        # Prefer profiles marked valid; fall back to all if none are valid.
        display = [(n, h) for n, h, v in profiles if v] or [(n, h) for n, h, _ in profiles]
        if sys.stdin.isatty():
            click.secho("\nMultiple Databricks profiles found.", fg="cyan", bold=True)
            click.echo("Pick one:\n")
            for i, (name, host) in enumerate(display, 1):
                num = click.style(f"  {i:2})", fg="bright_green", bold=True)
                click.echo(f"{num} {click.style(name, fg='cyan')}  "
                           f"{click.style(f'({host})', fg='bright_black')}")
            click.echo()
            idx = click.prompt(
                click.style("Profile number", fg="cyan", bold=True),
                type=click.IntRange(1, len(display)),
                default=1,
            )
            chosen = display[idx - 1][0]
            click.secho(
                f"\nTip: export DATABRICKS_CONFIG_PROFILE={chosen}  "
                f"(or pass --profile {chosen}) to skip this prompt.\n",
                fg="bright_black",
            )
            return _connect_workspace(chosen)
        # Non-interactive fallback: list profiles and exit cleanly.
        names = [n for n, _ in display]
        hint = "\n".join(f"  {n}" for n in names)
        raise click.ClickException(
            f"Could not resolve Databricks authentication.\n"
            f"Pass --profile to choose one:\n{hint}\n\n"
            f"Example:  apx-agent <command> --profile {names[0]}"
        ) from None


def _require_sdk(profile: str | None = None) -> "Any":
    """Return a WorkspaceClient, optionally scoped to a named config profile."""
    ws, _ = _connect_workspace(profile)
    return ws


def _fleet_resolve(
    ws: Any,
    *,
    catalog: str | None,
    schema: str | None,
    name_glob: str | None,
    where_exprs: tuple[str, ...],
    uc_names: tuple[str, ...],
) -> list[Any]:
    """List registered models and resolve them with the fleet selector."""
    from apx_agent import _fleet

    try:
        models = list(ws.registered_models.list(
            catalog_name=catalog, schema_name=schema, include_browse=False,
        ))
    except TypeError:
        models = list(ws.registered_models.list())  # type: ignore[call-arg]
    try:
        where = _fleet.parse_where(list(where_exprs))
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    return _fleet.resolve_agents(
        models, catalog=catalog, schema=schema, name_glob=name_glob,
        where=where, uc_names=list(uc_names) or None,
    )


def _fleet_select_options(f: Any) -> Any:
    """Reusable selection options shared by every fleet command."""
    f = click.option("--catalog", default=None, help="Restrict to a UC catalog.")(f)
    f = click.option("--schema", default=None,
                     help="Restrict to a UC schema (requires --catalog).")(f)
    f = click.option("--name", "name_glob", default=None,
                     help="Glob match against apx.agent.name (e.g. 'payroll-*').")(f)
    f = click.option("--where", "where_exprs", multiple=True,
                     help="Tag predicate key=value (repeatable, AND-ed).")(f)
    f = click.option("--uc-name", "uc_names", multiple=True,
                     help="Explicit registered-model name; bypasses filters.")(f)
    f = click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
                     help="Databricks config profile.")(f)
    return f


def _discover_local_agents(start: Path) -> list[dict[str, Any]]:
    """Agents DECLARED in the local project — AST-parsed from source, no auth.

    Walks up from ``start`` to the nearest ``pyproject.toml`` (the project root),
    then AST-parses every ``agent.py`` / ``agent_router.py`` under it for the
    agents it assigns. Returns ``[{agent_name, kind, tool_count, source}]``.
    A composition project yields one row per declared leaf + the root wrapper.
    """
    from ._ui_edit import _parse_agent_nodes

    root = start.resolve()
    for d in [root, *root.parents]:
        if (d / "pyproject.toml").is_file():
            root = d
            break
    skip = {".venv", "node_modules", ".worktrees", ".git", "__pycache__", "dist", "build"}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pattern in ("agent.py", "agent_router.py"):
        for p in sorted(root.rglob(pattern)):
            if any(part in skip for part in p.parts):
                continue
            rel = str(p.relative_to(root))
            if rel in seen:
                continue
            seen.add(rel)
            try:
                nodes = _parse_agent_nodes(p.read_text())
            except Exception:
                continue
            for n in nodes:
                rows.append({
                    "agent_name": n["name"],
                    "kind": n.get("wrapper") or "Agent",
                    "tool_count": len(n.get("tools") or []),
                    "source": rel,
                })
    rows.sort(key=lambda r: (r["source"], r["agent_name"]))
    return rows


@agents.command("list")
@click.option("--local", "local_only", is_flag=True,
              help="List agents DECLARED in the current project (offline — no Databricks calls).")
@click.option("--catalog", default=None,
              help="Restrict to a UC catalog. Default: any.")
@click.option("--schema", default=None,
              help="Restrict to a UC schema (requires --catalog).")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
@click.option("--apps-only", is_flag=True,
              help="Only agents deployed as Databricks Apps (skip the UC-registered scan).")
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks config profile (~/.databrickscfg).")
def list_agents_cmd(
    local_only: bool, catalog: str | None, schema: str | None, fmt: str,
    apps_only: bool, profile: str | None,
) -> None:
    """Discover apx-agents — locally declared or deployed to the workspace.

    ``--local`` lists agents DECLARED in the current project by AST-parsing its
    source — fully offline, no Databricks calls (handy in a fresh clone, CI, or
    before any deploy). Without it, lists agents DEPLOYED to the workspace from
    two sources merged into one table with a SERVING column: UC-registered
    models tagged ``apx.agent.name`` and agents running as Databricks Apps
    (probed via their A2A card). ``--apps-only`` skips the UC scan.
    """
    if local_only:
        if catalog or schema or apps_only:
            raise click.UsageError(
                "--local reads project source; it can't combine with "
                "--catalog/--schema/--apps-only (those scope the workspace scan)."
            )
        local_rows = _discover_local_agents(Path.cwd())
        if fmt == "json":
            click.echo(json.dumps(local_rows, indent=2, default=str))
            return
        if not local_rows:
            click.echo("No agents declared in this project.")
            click.echo("Scaffold one with:  apx-agent agents scaffold <NAME>")
            return
        name_w = max((len(r["agent_name"]) for r in local_rows), default=5)
        kind_w = max((len(r["kind"]) for r in local_rows), default=4)
        click.echo(f"{'AGENT':<{name_w}}  {'KIND':<{kind_w}}  TOOLS  SOURCE")
        for r in local_rows:
            click.echo(f"{r['agent_name']:<{name_w}}  {r['kind']:<{kind_w}}  "
                       f"{r['tool_count']:>5}  {r['source']}")
        return

    if schema and not catalog:
        raise click.UsageError("--schema requires --catalog.")
    if apps_only and (catalog or schema):
        raise click.UsageError("--apps-only can't be combined with --catalog/--schema (those scope the UC scan).")

    ws = _require_sdk(profile)
    agents_ = [] if apps_only else _fleet_resolve(
        ws, catalog=catalog, schema=schema, name_glob=None,
        where_exprs=(), uc_names=(),
    )
    rows: list[dict[str, Any]] = []
    # app_name -> row, so app discovery can fill in the live URL for an
    # apps-deploy that also registered a UC manifest (rather than duplicate it).
    by_app_name: dict[str, dict[str, Any]] = {}
    for a in agents_:
        resource_count = 0
        try:
            resource_count = len(json.loads(a.tags.get("apx.agent.metadata") or "{}")
                                 .get("resources") or [])
        except Exception:
            pass
        serving = "apps" if a.tags.get("apx.serving") == "apps" else "model-serving"
        row = {
            "agent_name": a.name,
            "serving": serving,
            "model_endpoint": a.model,
            "uc_name": a.uc_name,
            "url": None,
            "tool_count": a.tags.get("apx.agent.tool_count"),
            "resource_count": resource_count,
        }
        rows.append(row)
        if a.app_name:
            by_app_name[a.app_name] = row

    # App-discovered agents: probe Databricks Apps for their A2A card. Merge into
    # the UC manifest row when app_name matches; otherwise add a standalone row
    # (an apps-deploy that never registered a UC manifest, e.g. no UC name).
    from ._apps_discovery import discover_app_agents

    if fmt != "json":
        click.echo("(checking Databricks Apps for agents…)", err=True)
    app_agents = discover_app_agents(ws)
    for app in app_agents:
        existing = by_app_name.get(app.app_name)
        if existing is not None:
            existing["url"] = app.url
            continue
        rows.append({
            "agent_name": app.name,
            "serving": "apps",
            "model_endpoint": None,
            "uc_name": None,
            "url": app.url,
            "tool_count": app.tool_count,
            "resource_count": 0,
        })

    rows.sort(key=lambda r: (r["serving"], (r["agent_name"] or "")))

    if fmt == "json":
        click.echo(json.dumps(rows, indent=2, default=str))
        return

    if not rows:
        click.echo("No apx agents found in this workspace.")
        click.echo("Deploy one with:  apx-agent deploy")
        click.echo("Then run this command again to see it here.")
        return

    # Widths sized to the data so columns stay aligned regardless of name length.
    # UC NAME is dropped entirely when no agent has one (e.g. an all-Apps fleet),
    # and the long, variable ENDPOINT/URL goes last so it never shifts the
    # right-aligned TOOLS column around.
    show_uc = any(r["uc_name"] for r in rows)
    name_w = max(len("AGENT"), *(len(r["agent_name"] or "-") for r in rows))
    uc_w = max(len("UC NAME"), *(len(r["uc_name"] or "-") for r in rows)) if show_uc else 0

    header = f"{'AGENT':<{name_w}}  {'SERVING':<13}  "
    if show_uc:
        header += f"{'UC NAME':<{uc_w}}  "
    header += f"{'TOOLS':>5}  ENDPOINT"
    click.echo(header)

    for r in rows:
        endpoint = r["model_endpoint"] or r["url"] or "-"
        line = f"{(r['agent_name'] or '-'):<{name_w}}  {r['serving']:<13}  "
        if show_uc:
            line += f"{(r['uc_name'] or '-'):<{uc_w}}  "
        line += f"{(r['tool_count'] or '-'):>5}  {endpoint}"
        click.echo(line)

    # Tell the user how to act on a row — close the list→inspect gap.
    click.secho(
        "\n↳ inspect one:  apx-agent agents describe --app   (pick from a list)",
        fg="bright_black",
    )


# ---------------------------------------------------------------------------
# agents status — post-deploy health + provenance in one command (issue #410)
# ---------------------------------------------------------------------------


def _probe_local_sub_agents(cwd: Path) -> list[_doctor_mod.SubAgentProbe]:
    """Reachability of the local project's declared sub-agents (issue #445).

    Empty when ``cwd`` isn't an apx project or declares no ``sub_agents``.
    Never raises — ``agents status`` must not crash because a local
    pyproject is unreadable.
    """
    try:
        from ._inspection import _load_agent_config

        cfg = _load_agent_config(pyproject_path=cwd / "pyproject.toml")
    except Exception:
        return []
    if cfg is None or not cfg.sub_agents:
        return []
    return _doctor_mod.probe_sub_agents(cfg.sub_agents)


@agents.command("status")
@click.argument("uc_name", metavar="[NAME]", required=False)
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks config profile (~/.databrickscfg).")
def agents_status_cmd(uc_name: str | None, fmt: str, profile: str | None) -> None:
    """Is the deployed agent up, and what's running? — post-deploy health check.

    Resolves the latest deployed version from Unity Catalog, then probes the
    live target: a Databricks Apps deploy gets its app state plus a quick
    ``/readyz`` probe; a model-serving deploy gets its endpoint ready state.
    Prints target, state, URL, deployed version, and git provenance — with a
    drift line comparing the deployed commit to local HEAD when run inside a
    git repo.

    NAME is a three-part UC name (``catalog.schema.model``); when omitted, the
    current project's UC name resolves the same way ``apx-agent doctor``'s
    deploy-provenance check does.

    When the local project declares ``sub_agents``, a sub-agents section
    reports each declared peer's agent-card reachability (issue #445) —
    informational only, it never changes the exit code.

    Exit code: 0 when healthy; 1 when the deployed target exists but is not
    healthy (app not RUNNING, /readyz failed, endpoint not ready) or the
    target / UC could not be reached.
    """
    from ._apps_registry import APP_NAME_TAG, GIT_DIRTY_TAG, GIT_SHA_TAG, SERVING_TAG

    resolved = uc_name or _resolve_project_uc_name(Path.cwd())
    if resolved is None:
        raise click.ClickException(
            "No agent to check: pass a three-part UC name "
            "(apx-agent agents status catalog.schema.model), or run inside an "
            "apx project with [tool.apx.agent].registered_model (or a "
            "databricks.yml Apps bundle + non-placeholder catalog/schema)."
        )

    # 1. What's deployed, per the UC version ledger.
    try:
        from mlflow.tracking import MlflowClient
        versions = MlflowClient().search_model_versions(f"name='{resolved}'")
    except Exception as exc:
        raise click.ClickException(
            f"could not reach Unity Catalog for {resolved!r}: {exc}"
        ) from exc
    if not versions:
        raise click.ClickException(
            f"nothing deployed under {resolved!r} — no registered versions. "
            "Deploy with `apx-agent agents deploy` first."
        )
    latest = max(versions, key=lambda v: int(v.version))
    version = str(latest.version)
    tags: dict[str, str] = {}
    ws, cfg = _connect_workspace(profile)
    try:
        model = ws.registered_models.get(resolved)
        tags = {t.key: t.value for t in (getattr(model, "tags", None) or [])}
    except Exception:
        pass  # version tags alone usually carry the target — keep going
    tags.update(dict(getattr(latest, "tags", None) or {}))  # version tags win

    # 2. Probe the live target.
    target = "apps" if tags.get(SERVING_TAG) == "apps" else "model-serving"
    readyz: bool | None = None
    readyz_detail: str | None = None
    if target == "apps":
        name = tags.get(APP_NAME_TAG)
        if not name:
            raise click.ClickException(
                f"{resolved} version {version} is an Apps deploy but records "
                f"no {APP_NAME_TAG} tag — cannot locate the App. Re-deploy, or "
                "re-stamp tags with `apx-agent agents adopt --app <name>`."
            )
        try:
            app = ws.apps.get(name)
        except Exception as exc:
            raise click.ClickException(f"could not reach app {name!r}: {exc}") from exc
        raw_state = getattr(getattr(app, "app_status", None), "state", None)
        state = (
            str(getattr(raw_state, "value", raw_state))
            if raw_state is not None else "UNKNOWN"
        )
        url = getattr(app, "url", None)
        if url:
            # Fewer attempts than the deploy gate: status should be snappy —
            # the app has been up for a while (or plainly isn't).
            result = _check_readyz(url, profile=profile, attempts=2, delay_s=2.0)
            readyz = result.is_ready
            if not result.is_ready:
                readyz_detail = str(result.checks)
        healthy = state == "RUNNING" and readyz is True
    else:
        name = tags.get("apx.agent.model")
        if not name:
            raise click.ClickException(
                f"{resolved} version {version} records no apx.agent.model tag "
                "— cannot locate its serving endpoint. Re-deploy with this "
                "apx-agent version to stamp it."
            )
        try:
            endpoint = ws.serving_endpoints.get(name)
        except Exception as exc:
            raise click.ClickException(
                f"could not reach serving endpoint {name!r}: {exc}"
            ) from exc
        ready = getattr(getattr(endpoint, "state", None), "ready", None)
        state = (
            str(getattr(ready, "value", ready)) if ready is not None else "UNKNOWN"
        )
        url = (
            f"{cfg.host.rstrip('/')}/serving-endpoints/{name}/invocations"
            if cfg.host else None
        )
        # ponytail: endpoint state + version is the ceiling for this iteration
        # — no smoke invocation; state.ready already answers "is it up".
        healthy = state == "READY"

    # 3. Provenance + drift vs local HEAD (issue #403 tags).
    git_sha = tags.get(GIT_SHA_TAG)
    dirty = tags.get(GIT_DIRTY_TAG) == "true"
    local_sha = _git_head_sha(Path.cwd())
    drift: str | None = None  # None: not a git repo, or no recorded provenance
    drift_line: str | None = None
    if git_sha and local_sha:
        if git_sha == local_sha:
            drift = "match"
            drift_line = (
                f"none — deployed commit matches local HEAD ({local_sha[:12]})"
            )
        else:
            drift = "drift"
            drift_line = (
                f"deployed {git_sha[:12]} != local HEAD {local_sha[:12]} "
                "— re-deploy to ship HEAD"
            )

    # 4. Local project sub-agents (issue #445): probe each declared peer's
    #    agent card. Informational — never changes `healthy` or the exit
    #    code (a down peer degrades the orchestrator, it doesn't kill it).
    sub_probes = _probe_local_sub_agents(Path.cwd())

    if fmt == "json":
        click.echo(json.dumps({
            "uc_name": resolved,
            "target": target,
            "name": name,
            "state": state,
            "url": url,
            "version": version,
            "git_sha": git_sha,
            "git_dirty": dirty if git_sha else None,
            "local_sha": local_sha,
            "drift": drift,
            "readyz": readyz,
            "sub_agents": [p.as_dict() for p in sub_probes],
            "healthy": healthy,
        }, indent=2))
    else:
        rows = [
            ("AGENT", resolved),
            ("TARGET", target),
            ("APP" if target == "apps" else "ENDPOINT", name),
            ("STATE", state),
        ]
        if target == "apps":
            if readyz is True:
                rows.append(("READYZ", "ready"))
            elif readyz is False:
                rows.append(("READYZ", f"failed — {readyz_detail}"))
            else:
                rows.append(("READYZ", "not probed (app has no URL)"))
        rows.append(("URL", url if url else "-"))
        rows.append(("VERSION", version))
        if git_sha:
            sha_display = f"{git_sha[:12]} (dirty tree)" if dirty else git_sha[:12]
        else:
            sha_display = "(none recorded — deployed before provenance stamping)"
        rows.append(("GIT SHA", sha_display))
        if drift_line:
            rows.append(("DRIFT", drift_line))
        rows.append(("HEALTHY", "yes" if healthy else "no"))
        width = max(len(key) for key, _ in rows)
        for key, value in rows:
            click.echo(f"{key:<{width}}  {value}")
        if sub_probes:
            click.echo(f"\nSub-agents ({len(sub_probes)}):")
            for p in sub_probes:
                if p.reachable:
                    detail = f"reachable ({p.name})" if p.name else "reachable"
                else:
                    detail = f"unreachable ({p.error})"
                click.echo(f"  {p.url}  {detail}")

    if not healthy:
        raise click.exceptions.Exit(1)


# ---------------------------------------------------------------------------
# agents register — single-agent UC manifest backfill (issue #418)
# ---------------------------------------------------------------------------


@agents.command("register")
@click.option("--module", default="agent:agent", help="Agent module spec.")
@click.option(
    "--uc-name", default=None,
    help="UC three-part name (catalog.schema.model) to register the manifest "
         "under. Overrides [tool.apx.agent].registered_model and the "
         "catalog/schema-composed default.",
)
@click.option(
    "--bundle-target", default="dev",
    help="DAB target recorded on the manifest's apx.apps.bundle_target tag. "
         "Default: dev.",
)
@click.option(
    "--yes", "-y", "assume_yes", is_flag=True,
    help="Skip the confirmation prompt.",
)
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks config profile (~/.databrickscfg).")
@click.option("--json", "as_json", is_flag=True,
              help="Emit a single JSON result object on stdout "
                   "(ok:false + error on failure).")
def register_agent_cmd(
    module: str,
    uc_name: str | None,
    bundle_target: str,
    assume_yes: bool,
    profile: str | None,
    as_json: bool,
) -> None:
    """Backfill the UC version manifest for an already-live Apps deploy.

    Deploy-time registration is best-effort: when it fails, the App stays
    live but has NO ledger entry — invisible to `agents list` / topology /
    watchdog (issue #418). Run this from the project directory to register
    the manifest for one agent, without a workspace-wide `fleet backfill`.

    Resolves the app name and UC name exactly as `agents deploy --target
    apps` does, verifies the App actually exists in the workspace (refuses
    to ledger a nonexistent app), then registers the manifest with fresh
    git/lock provenance tags. Unlike the deploy-time step, a registration
    failure here IS fatal — a backfill has no green deploy to protect.
    """
    with _json_cli_errors(as_json):
        cwd = Path.cwd()
        config = _read_apx_agent_config()
        profile = profile or config.get("profile")
        if profile:
            # register_apps_manifest logs via ambient mlflow auth — make the
            # chosen profile the ambient one, like `uc publish` does.
            os.environ["DATABRICKS_CONFIG_PROFILE"] = profile

        _bundle_key, app_name = _resolve_app_name(_read_databricks_yml(cwd))
        resolved_uc = _resolve_apps_uc_name(config, app_name, override=uc_name)
        if resolved_uc is None:
            raise click.ClickException(
                "no UC model name resolved. Set "
                "[tool.apx.agent].registered_model (or a non-placeholder "
                "catalog + schema) in pyproject.toml, or pass --uc-name."
            )
        model = config.get("model")
        if not model:
            raise click.ClickException(
                "no LLM model in [tool.apx.agent].model to log "
                f"{resolved_uc} with. Set a model first."
            )

        # Refuse to ledger an app that isn't actually deployed.
        ws, _cfg = _connect_workspace(profile)
        try:
            ws.apps.get(app_name)
        except Exception as exc:
            raise click.ClickException(
                f"app {app_name!r} not found in the workspace — refusing to "
                f"register a manifest for an app that isn't deployed: {exc}\n"
                "Deploy it first: apx-agent agents deploy --target apps"
            ) from exc

        if not assume_yes:
            click.echo("Will register a UC version manifest:")
            click.echo(f"  • app:       {app_name} (verified live)")
            click.echo(f"  • UC name:   {resolved_uc}")
            click.echo(f"  • DAB target: {bundle_target}")
            click.confirm("Proceed?", abort=True)

        agent = _load_finalized_agent(module)
        from ._apps_registry import register_apps_manifest
        try:
            res = register_apps_manifest(
                agent,
                uc_name=resolved_uc,
                model=model,
                app_name=app_name,
                bundle_target=bundle_target,
                agent_name=config.get("name") or app_name,
                extra_version_tags=_provenance_version_tags(cwd),
            )
        except Exception as exc:
            raise click.ClickException(
                f"UC registration failed: {exc}"
            ) from exc

        if as_json:
            click.echo(json.dumps({
                "ok": True,
                "uc_name": res.uc_name,
                "version": res.version,
                "app_name": res.app_name,
                "bundle_target": bundle_target,
            }))
        else:
            click.echo(
                f"registered {res.uc_name} version {res.version} "
                f"(manifest for App {res.app_name}; not promoted to serving)"
            )


# ---------------------------------------------------------------------------
# agents delete — remove UC model + serving endpoint + Databricks App
# ---------------------------------------------------------------------------


def _registry_row_age(updated_at: Any) -> str:
    """Render a registry row's ``updated_at`` (epoch seconds) as an age.

    Registry rows carry when they were last advertised; surfacing that age in
    the dependents warning tells the operator whether a referencing row is
    live routing or a stale leftover (#446).
    """
    try:
        days = int((time.time() - float(updated_at)) // 86400)
    except (TypeError, ValueError):
        return "unknown age"
    if days <= 0:
        return "today"
    return f"{days}d ago"


@agents.command("delete")
@click.option(
    "--uc-name", required=True,
    help="Three-part UC name of the registered model (catalog.schema.model).",
)
@click.option(
    "--endpoint", "endpoint_name", default=None,
    help="Model Serving endpoint name. Auto-detected from UC tags when omitted.",
)
@click.option(
    "--app", "app_name", default=None,
    help="Databricks App name to delete. Auto-detected from UC tags when omitted.",
)
@click.option(
    "--yes", "-y", "assume_yes", is_flag=True,
    help="Skip confirmation prompt.",
)
@click.option(
    "--purge", is_flag=True,
    help="Also delete the discoverable deploy leftovers: the auto-created "
         "MLflow experiment (/Users/<you>/<app>-<target>), any soaking "
         "canary App (<app>-canary-*), and the DAB workspace files "
         "(~/.bundle/<app>/<target>). Lakebase session tables are NEVER "
         "dropped — data deletion stays manual; a notice names them.",
)
@click.option("--registry-table", default=None,
              help="UC Delta agent registry table for the advertise-row cleanup. "
                   "Defaults to [tool.apx.agent].registry_table or main.apx.agent_registry.")
@click.option("--tools-table", default=None,
              help="UC Delta tools registry table for the advertise-row cleanup. "
                   "Defaults to [tool.apx.agent].tools_table or main.apx.agent_tools.")
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks CLI profile (~/.databrickscfg).")
def delete_agent_cmd(
    uc_name: str,
    endpoint_name: str | None,
    app_name: str | None,
    assume_yes: bool,
    purge: bool,
    registry_table: str | None,
    tools_table: str | None,
    profile: str | None,
) -> None:
    """Delete a deployed agent — UC model registration, serving endpoint, and app.

    Removes:

    \b
      1. The Model Serving endpoint (--endpoint or auto-detected from UC tags)
      2. The registered model in Unity Catalog (--uc-name)
      3. The Databricks App (--app or auto-detected from UC tags)
      4. The agent's advertise-registry rows (agent registry + tools registry
         tables), when those tables exist — so discovery stops advertising
         a deleted agent

    Before the confirmation prompt, the advertise registry is also scanned
    (best-effort) for OTHER agents whose rows reference this one — deleting
    an agent that others route to is silent breakage, so any hits are listed
    with how stale their registry row is.

    With --purge, also removes what is discoverable from tags and naming
    conventions (best-effort, aggregated errors):

    \b
      5. The auto-created MLflow experiment — the apx.mlflow.experiment_id
         tag when recorded, else /Users/<you>/<app>-<target>
      6. Any soaking canary App (<app>-canary-<version>)
      7. The DAB deploy root in the workspace (~/.bundle/<app>/<target>)

    Lakebase session-store tables are never dropped automatically — the
    command prints the tables to DROP instead. Anything --purge cannot
    derive with confidence is reported as "not removed: <reason>".

    Pass --yes to skip the interactive confirmation prompt.
    """
    ws, ws_cfg = _connect_workspace(profile)

    # Resolve endpoint and app from UC tags when not explicitly given.
    # --purge always needs the tags (bundle_target, experiment id).
    resolved_endpoint = endpoint_name
    resolved_app = app_name
    uc_tags: dict[str, str] = {}
    if purge or not resolved_endpoint or not resolved_app:
        try:
            model = ws.registered_models.get(uc_name)
            uc_tags = {t.key: t.value for t in (getattr(model, "tags", None) or [])}
        except Exception:
            pass
    if not resolved_endpoint:
        resolved_endpoint = uc_tags.get("apx.agent.model")
    if not resolved_app:
        resolved_app = uc_tags.get("apx.apps.app_name")

    # Advertise-registry cleanup + dependents scan (#446). Best-effort: only
    # attempted when the registry tables affirmatively exist — an agent that
    # was never advertised has nothing to deregister.
    from apx_agent._publish import _slug, find_registry_dependents, remove_from_registry

    cfg = _read_apx_agent_config()
    registry_table = registry_table or cfg.get("registry_table") or "main.apx.agent_registry"
    tools_table = tools_table or cfg.get("tools_table") or "main.apx.agent_tools"
    assert registry_table is not None and tools_table is not None

    # The registry keys rows on _slug(f"{name}_{host_short}") — mirror the
    # advertise-side derivation for both names the victim may be listed under.
    host = getattr(ws_cfg, "host", None)
    host_short = host.replace("https://", "").split(".")[0] if isinstance(host, str) else ""
    victim_agent_ids = list(dict.fromkeys(
        _slug(f"{name}_{host_short}")
        for name in (resolved_endpoint, resolved_app) if name
    ))

    registry_ready = False
    if not victim_agent_ids:
        click.echo(
            "Notice: registry cleanup skipped (no endpoint or app name to "
            "derive the agent's registry rows from).",
        )
    else:
        try:
            registry_ready = (
                ws.tables.exists(registry_table).table_exists is True
                and ws.tables.exists(tools_table).table_exists is True
            )
        except Exception as exc:
            click.echo(
                f"Notice: registry cleanup skipped (could not check "
                f"{registry_table} / {tools_table}: {exc})",
            )
        else:
            if not registry_ready:
                click.echo(
                    f"Notice: registry cleanup skipped ({registry_table} / "
                    f"{tools_table} not found — nothing advertised).",
                )

    dependents: list[dict[str, Any]] = []
    if registry_ready:
        try:
            dependents = find_registry_dependents(
                agent_ids=victim_agent_ids,
                references=[n for n in (uc_name, resolved_endpoint, resolved_app) if n],
                registry_table=registry_table, tools_table=tools_table, ws=ws,
            )
        except Exception as exc:
            click.echo(
                f"Notice: could not scan the registry for dependents: {exc}",
                err=True,
            )

    # --purge: enumerate the discoverable leftovers BEFORE the confirmation
    # prompt so the summary lists everything that will (and won't) go.
    purge_notices: list[str] = []
    experiment_id: str | None = None
    experiment_label: str | None = None
    canary_apps: list[str] = []
    bundle_path: str | None = None
    if purge:
        bundle_target = uc_tags.get("apx.apps.bundle_target") or "dev"
        user_name: str | None = None
        try:
            user_name = ws.current_user.me().user_name
        except Exception as exc:
            purge_notices.append(
                "not removed: MLflow experiment + bundle workspace files "
                f"(could not resolve current user: {exc})"
            )

        # a. MLflow experiment — explicit tag first, else the canonical path
        # deploy --auto-experiment creates (/Users/<you>/<app>-<target>).
        experiment_id = uc_tags.get("apx.mlflow.experiment_id")
        if experiment_id:
            experiment_label = (
                f"MLflow experiment id {experiment_id} "
                "(from apx.mlflow.experiment_id tag)"
            )
        elif user_name and resolved_app:
            exp_path = f"/Users/{user_name}/{resolved_app}-{bundle_target}"
            try:
                got = ws.experiments.get_by_name(exp_path)
                experiment_id = getattr(
                    getattr(got, "experiment", None), "experiment_id", None,
                )
            except Exception:
                experiment_id = None
            if experiment_id:
                experiment_label = f"MLflow experiment: {exp_path} (id {experiment_id})"
            else:
                purge_notices.append(
                    f"not removed: MLflow experiment (nothing at {exp_path})"
                )
        elif user_name:
            purge_notices.append(
                "not removed: MLflow experiment (no App name to derive "
                "/Users/<you>/<app>-<target> from)"
            )

        # b. Soaking canary Apps — canary deploy names them
        # <app>-canary-<version> (see _canary_apps.canary_app_name).
        if resolved_app:
            prefix = f"{resolved_app}-canary-"
            try:
                canary_apps = sorted(
                    a.name for a in ws.apps.list()
                    if a.name and a.name.startswith(prefix)
                )
            except Exception as exc:
                purge_notices.append(
                    f"not removed: canary Apps (could not list Apps: {exc})"
                )
        else:
            purge_notices.append(
                "not removed: canary Apps (no App name to derive "
                "<app>-canary-* from)"
            )

        # c. DAB workspace files — scaffolded bundles set bundle.name to the
        # app name, so the deploy root is ~/.bundle/<app>/<target>. Only
        # delete when that exact path exists in the workspace.
        if user_name and resolved_app:
            candidate = f"/Users/{user_name}/.bundle/{resolved_app}/{bundle_target}"
            try:
                ws.workspace.get_status(candidate)
                bundle_path = candidate
            except Exception:
                purge_notices.append(
                    f"not removed: bundle workspace files (nothing at {candidate})"
                )
        elif user_name:
            purge_notices.append(
                "not removed: bundle workspace files (no App name to derive "
                "~/.bundle/<app>/<target> from)"
            )

        # d. Lakebase session tables — data deletion stays manual by design;
        # never drop tables, just say what to drop. (# ponytail)
        db_hint = (
            re.sub(r"[^a-z0-9_]", "_", resolved_app.lower()).strip("_")
            if resolved_app else "<app-slug>"
        )
        purge_notices.append(
            "not removed: Lakebase session tables (data deletion stays "
            "manual) — if the scaffold session store was used, run "
            "DROP TABLE apx_conversations, apx_conversation_items in "
            f"Lakebase database {db_hint!r} yourself."
        )

    # Build a summary of what will be deleted for the confirmation prompt.
    to_delete: list[str] = [f"  • UC registered model: {uc_name}"]
    if resolved_endpoint:
        to_delete.append(f"  • Model Serving endpoint: {resolved_endpoint}")
    if resolved_app:
        to_delete.append(f"  • Databricks App: {resolved_app}")
    if experiment_label:
        to_delete.append(f"  • {experiment_label}")
    for capp in canary_apps:
        to_delete.append(f"  • Canary App: {capp}")
    if bundle_path:
        to_delete.append(f"  • Bundle workspace files: {bundle_path}")
    if registry_ready:
        to_delete.append(
            f"  • Advertise-registry rows in {registry_table} + {tools_table} "
            f"(agent_id: {', '.join(victim_agent_ids)})"
        )

    for notice in purge_notices:
        click.echo(notice)

    # Dependents warning — deleting an agent that others route to is silent
    # breakage, so surface it (with row staleness) before asking to proceed.
    if dependents:
        seen: dict[tuple[str, str], float | None] = {}
        for dep in dependents:
            key = (str(dep["agent"] or "(unknown agent)"), str(dep["via"] or "(unknown reference)"))
            seen.setdefault(key, dep["updated_at"])
        n_agents = len({agent for agent, _ in seen})
        click.echo(
            f"Warning: {n_agents} other agent(s) reference this one — "
            "deleting it will break their routing:"
        )
        for (agent, via), updated_at in sorted(seen.items()):
            click.echo(
                f"  • {agent} (via {via}, last advertised "
                f"{_registry_row_age(updated_at)})"
            )

    if not assume_yes:
        click.echo("This will permanently delete:")
        for line in to_delete:
            click.echo(line)
        click.confirm("Proceed?", abort=True)

    errors: list[str] = []

    # 1. Delete the serving endpoint first (it holds the model version lock).
    if resolved_endpoint:
        try:
            ws.serving_endpoints.delete(resolved_endpoint)
            click.echo(f"Deleted endpoint: {resolved_endpoint}")
        except Exception as exc:
            errors.append(f"endpoint {resolved_endpoint!r}: {exc}")
            click.echo(f"Warning: could not delete endpoint {resolved_endpoint!r}: {exc}", err=True)

    # 2. Delete the registered model (all versions).
    try:
        ws.registered_models.delete(uc_name)
        click.echo(f"Deleted UC model: {uc_name}")
    except Exception as exc:
        errors.append(f"UC model {uc_name!r}: {exc}")
        click.echo(f"Warning: could not delete UC model {uc_name!r}: {exc}", err=True)

    # 3. Delete the Databricks App (best-effort).
    if resolved_app:
        try:
            ws.apps.delete(resolved_app)
            click.echo(f"Deleted app: {resolved_app}")
        except Exception as exc:
            errors.append(f"app {resolved_app!r}: {exc}")
            click.echo(f"Warning: could not delete app {resolved_app!r}: {exc}", err=True)

    # 4. Remove the advertise-registry rows (best-effort, aggregated) so
    # discovery stops advertising the deleted agent (#446).
    if registry_ready:
        for victim_id in victim_agent_ids:
            try:
                remove_from_registry(
                    agent_id=victim_id,
                    registry_table=registry_table, tools_table=tools_table, ws=ws,
                )
                click.echo(f"Removed advertise-registry rows for agent_id: {victim_id}")
            except Exception as exc:
                errors.append(f"registry rows {victim_id!r}: {exc}")
                click.echo(
                    f"Warning: could not remove registry rows for {victim_id!r}: {exc}",
                    err=True,
                )

    # 5. --purge extras (lists/ids are only populated when --purge was passed).
    for capp in canary_apps:
        try:
            ws.apps.delete(capp)
            click.echo(f"Deleted canary app: {capp}")
        except Exception as exc:
            errors.append(f"canary app {capp!r}: {exc}")
            click.echo(f"Warning: could not delete canary app {capp!r}: {exc}", err=True)

    if experiment_id:
        try:
            ws.experiments.delete_experiment(experiment_id)
            click.echo(f"Deleted MLflow experiment: {experiment_id}")
        except Exception as exc:
            errors.append(f"experiment {experiment_id!r}: {exc}")
            click.echo(
                f"Warning: could not delete experiment {experiment_id!r}: {exc}",
                err=True,
            )

    if bundle_path:
        try:
            ws.workspace.delete(bundle_path, recursive=True)
            click.echo(f"Deleted bundle workspace files: {bundle_path}")
        except Exception as exc:
            errors.append(f"bundle files {bundle_path!r}: {exc}")
            click.echo(
                f"Warning: could not delete bundle files {bundle_path!r}: {exc}",
                err=True,
            )

    if errors:
        raise click.ClickException(
            f"{len(errors)} deletion(s) failed (see warnings above). "
            "Resources may need manual cleanup in the workspace UI."
        )


@uc.command("catalogs")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
@click.pass_context
def list_catalogs_cmd(ctx: click.Context, fmt: str) -> None:
    """List Unity Catalog catalogs accessible in this workspace."""
    ws = _require_sdk((ctx.obj or {}).get("profile"))
    catalogs = _ws_list_catalogs(ws)

    if fmt == "json":
        click.echo(json.dumps(catalogs))
        return

    if not catalogs:
        click.echo("No catalogs found.")
        click.echo("Check that Unity Catalog is enabled and you have USE CATALOG on at least one catalog.")
        click.echo("Run `apx-agent doctor` to verify workspace access.")
        return
    for name in catalogs:
        click.echo(name)


@uc.command("schemas")
@click.argument("catalog")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
@click.pass_context
def list_schemas_cmd(ctx: click.Context, catalog: str, fmt: str) -> None:
    """List schemas in CATALOG."""
    ws = _require_sdk((ctx.obj or {}).get("profile"))
    schemas = _ws_list_schemas(ws, catalog)

    if fmt == "json":
        click.echo(json.dumps(schemas))
        return

    if not schemas:
        click.echo(f"No schemas found in {catalog}.")
        click.echo(f"Check that you have USE SCHEMA on at least one schema in {catalog}.")
        return
    for name in schemas:
        click.echo(name)


@uc.command("tables")
@click.argument("catalog")
@click.argument("schema")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
@click.pass_context
def list_tables_cmd(ctx: click.Context, catalog: str, schema: str, fmt: str) -> None:
    """List tables in CATALOG.SCHEMA."""
    ws = _require_sdk((ctx.obj or {}).get("profile"))
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
        click.echo(f"Check that you have SELECT on at least one table in {catalog}.{schema}.")
        return
    click.echo(f"{'TABLE':<40}  {'TYPE':<16}  COMMENT")
    for t in tables:
        name = getattr(t, "name", "-") or "-"
        ttype = str(getattr(t, "table_type", "")).replace("TableType.", "") or "-"
        comment = (getattr(t, "comment", None) or "")[:60]
        click.echo(f"{name:<40}  {ttype:<16}  {comment}")


@uc.command("tools")
@click.argument("catalog")
@click.argument("schema")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
@click.pass_context
def list_tools_cmd(ctx: click.Context, catalog: str, schema: str, fmt: str) -> None:
    """List UC functions in CATALOG.SCHEMA (available as agent tools).

    These are the functions ``apx-agent publish-tools`` registers and that
    DataAgent / CoworkerAgent can call when ``include_functions=True``.
    """
    ws = _require_sdk((ctx.obj or {}).get("profile"))
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
        click.echo(f"Publish tools from your agent with:  apx-agent publish-tools")
        return
    click.echo(f"{'FUNCTION':<50}  COMMENT")
    for f in fns:
        name = getattr(f, "name", "-") or "-"
        comment = (getattr(f, "comment", None) or "")[:70]
        click.echo(f"{name:<50}  {comment}")


# ---------------------------------------------------------------------------
# uc validate — check UC grants needed before deploy
# ---------------------------------------------------------------------------


@uc.command("validate")
@click.option("--catalog", required=True, help="Catalog to validate.")
@click.option("--schema", required=True, help="Schema to validate.")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
@click.pass_context
def uc_validate_cmd(ctx: click.Context, catalog: str, schema: str, fmt: str) -> None:
    """Check Unity Catalog permissions required to deploy an agent.

    Verifies the current user has:

    \b
      • USE CATALOG on CATALOG
      • USE SCHEMA on CATALOG.SCHEMA
      • SELECT on at least one table in CATALOG.SCHEMA (DataAgent reads)
      • CREATE MODEL on CATALOG.SCHEMA (log_agent registers a model)

    Exits non-zero if any required permission appears to be missing.
    """
    ws = _require_sdk((ctx.obj or {}).get("profile"))

    checks: list[dict[str, Any]] = []

    def _check(name: str, fn: Any) -> bool:
        try:
            result = fn()
            ok = bool(result)
            checks.append({"check": name, "status": "ok" if ok else "missing", "detail": None})
            return ok
        except Exception as exc:
            checks.append({"check": name, "status": "error", "detail": str(exc)})
            return False

    # USE CATALOG — enumerate catalogs and see if ours appears.
    # _ws_list_catalogs returns plain strings; ws.catalogs.list() returns objects.
    _check(
        f"USE CATALOG on {catalog}",
        lambda: catalog.lower() in [s.lower() for s in (_ws_list_catalogs(ws) or [])],
    )

    # USE SCHEMA — enumerate schemas.
    # _ws_list_schemas returns plain strings.
    _check(
        f"USE SCHEMA on {catalog}.{schema}",
        lambda: schema.lower() in [s.lower() for s in (_ws_list_schemas(ws, catalog) or [])],
    )

    # SELECT on at least one table — listing tables requires SELECT.
    _check(
        f"SELECT on tables in {catalog}.{schema}",
        lambda: len(_ws_list_tables(ws, catalog, schema)) > 0,
    )

    # CREATE MODEL — try to list registered models in this schema.
    _check(
        f"CREATE MODEL / USE SCHEMA for model registration in {catalog}.{schema}",
        lambda: ws.registered_models.list(catalog_name=catalog, schema_name=schema) is not None,
    )

    missing = [c for c in checks if c["status"] != "ok"]

    if fmt == "json":
        click.echo(json.dumps({
            "catalog": catalog,
            "schema": schema,
            "checks": checks,
            "all_ok": not missing,
        }, indent=2))
        if missing:
            raise SystemExit(1)
        return

    all_ok = not missing
    for c in checks:
        icon = "✓" if c["status"] == "ok" else ("✗" if c["status"] == "missing" else "!")
        line = f"  [{icon}] {c['check']}"
        if c["detail"]:
            line += f"  ({c['detail'][:80]})"
        click.echo(line)

    if all_ok:
        click.echo(f"\nAll checks passed — {catalog}.{schema} is ready for deploy.")
    else:
        click.echo(
            f"\n{len(missing)} check(s) failed. "
            "Grant the missing privileges or re-run with --profile to use a different account.",
            err=True,
        )
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# cost — DBU / $ per agent or endpoint over a lookback window
# ---------------------------------------------------------------------------


@agents.command()
@click.option("--agent", "agent_name", default=None,
              help="Agent name. Resolves to the serving endpoint of the same name.")
@click.option("--endpoint", default=None,
              help="Serving endpoint name. Use this when the endpoint name differs from the agent name.")
@click.option("--hours", default=24, type=int,
              help="Lookback window in hours. Default 24.")
@click.option("--warehouse-id", default=None,
              help="SQL warehouse to run the system-tables query on.")
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks CLI profile (~/.databrickscfg).")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def cost(
    agent_name: str | None,
    endpoint: str | None,
    hours: int,
    warehouse_id: str | None,
    profile: str | None,
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

    from apx_agent import cost_for_agent

    ws, _ = _connect_workspace(profile)
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


@traces.command("export")
@click.option("--experiment", default=None,
              help="MLflow experiment. Falls back to [tool.apx.agent].experiment.")
@click.option("--table", "target_table", required=True,
              help="Three-part UC name (catalog.schema.table) for the destination Delta table.")
@click.option("--hours", default=24, type=int, help="Lookback window in hours.")
@click.option("--warehouse-id", default=None, help="SQL warehouse for the Delta writes.")
@click.option("--max-traces", default=1000, type=int, help="Max traces per export run.")
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks CLI profile (~/.databrickscfg).")
def export_traces_cmd(
    experiment: str | None,
    target_table: str,
    hours: int,
    warehouse_id: str | None,
    max_traces: int,
    profile: str | None,
) -> None:
    """Export MLflow traces to a Delta table for analytics."""
    effective_experiment = experiment or _read_apx_agent_config().get("experiment")
    if not effective_experiment:
        raise click.UsageError(
            "Pass --experiment NAME or set [tool.apx.agent].experiment in pyproject.toml."
        )

    from apx_agent import export_traces

    ws, _ = _connect_workspace(profile)
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
# uc topology
# ---------------------------------------------------------------------------


@uc.command()
@click.option("--catalog", default=None, help="Restrict to a UC catalog.")
@click.option("--schema", default=None, help="Restrict to a UC schema (requires --catalog).")
@click.option(
    "--format", "fmt", type=click.Choice(["mermaid", "graphviz"]),
    default="mermaid", help="Output format. Default: mermaid.",
)
@click.option("--output", "output_file", default=None,
              type=click.Path(dir_okay=False),
              help="Write the rendered diagram to FILE instead of stdout.")
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks CLI profile (~/.databrickscfg).")
def topology(
    catalog: str | None,
    schema: str | None,
    fmt: str,
    output_file: str | None,
    profile: str | None,
) -> None:
    """Render the multi-agent endpoint graph from UC tags."""
    if schema and not catalog:
        raise click.UsageError("--schema requires --catalog.")

    from apx_agent import discover_topology, render_topology

    ws, _ = _connect_workspace(profile)
    topo = discover_topology(ws, catalog=catalog, schema=schema)

    if not topo.nodes:
        scope = ""
        if catalog:
            scope = f" in {catalog}" + (f".{schema}" if schema else "")
        click.echo(f"No apx-tagged agents found{scope}.")
        click.echo("Agents appear here after `apx-agent deploy` writes apx.agent.* UC tags.")
        click.echo("Deploy one with:  apx-agent deploy")
        return

    text = render_topology(topo, format=cast("Literal['mermaid', 'graphviz']", fmt))

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


@eval_group.command("chain")
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
    if not report.cases:
        click.echo("  No cases ran. Check that the evalset file contains at least one row with a 'request' field.")
        return
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
# eval report — compare eval runs across versions
# ---------------------------------------------------------------------------


@eval_group.command("report")
@click.option("--experiment", default=None,
              help="MLflow experiment name. Falls back to [tool.apx.agent].experiment.")
@click.option("--versions", "n_versions", default=5, type=int,
              help="Number of most-recent eval runs to compare. Default 5.")
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]),
    default="text", help="Output format.",
)
def eval_report_cmd(
    experiment: str | None,
    n_versions: int,
    fmt: str,
) -> None:
    """Compare eval metrics across the most-recent eval runs in an experiment.

    Prints a table of run ID, start time, and key metrics (pass_rate, avg_latency_ms,
    token_count) so you can spot regressions across versions at a glance.

    Metrics are read from mlflow.search_runs — they match what ``apx-agent eval run``
    logs via Mosaic AI Agent Evaluation (``response/llm_judged/correctness/ratio`` and
    similar) and standard mlflow.log_metric calls.
    """
    try:
        import mlflow
    except ImportError as e:
        raise click.ClickException(
            "apx-agent eval report requires mlflow. Install with: uv add 'apx-agent[eval]'"
        ) from e

    effective_experiment = experiment or _read_apx_agent_config().get("experiment")
    if not effective_experiment:
        raise click.UsageError(
            "Pass --experiment NAME or set [tool.apx.agent].experiment in pyproject.toml."
        )

    try:
        exp = mlflow.get_experiment_by_name(effective_experiment)
    except Exception as e:
        raise click.ClickException(f"Could not find experiment {effective_experiment!r}: {e}") from e
    if exp is None:
        raise click.ClickException(f"Experiment {effective_experiment!r} not found.")

    # Candidate metric keys from Mosaic AI Agent Evaluation and apx-agent eval.
    _METRIC_CANDIDATES = [
        "response/llm_judged/correctness/ratio",
        "response/llm_judged/relevance/ratio",
        "response/llm_judged/safety/ratio",
        "pass_rate",
        "latency_ms",
        "avg_latency_ms",
    ]

    try:
        runs_df = mlflow.search_runs(  # type: ignore[attr-defined]
            experiment_ids=[exp.experiment_id],
            order_by=["start_time DESC"],
            max_results=n_versions,
        )
    except Exception as e:
        raise click.ClickException(f"mlflow.search_runs failed: {e}") from e

    # Normalise: search_runs returns a DataFrame or list depending on version.
    if hasattr(runs_df, "to_dict"):
        records: list[dict[str, Any]] = runs_df.to_dict(orient="records")  # type: ignore[union-attr]
    else:
        records = [
            {
                "run_id": getattr(r, "info", r).run_id if hasattr(r, "info") else r.get("run_id"),
                "start_time": getattr(r, "info", r).start_time if hasattr(r, "info") else r.get("start_time"),
                **{f"metrics.{k}": v for k, v in (getattr(getattr(r, "data", None), "metrics", None) or {}).items()},
            }
            for r in cast("list[Any]", runs_df or [])
        ]

    if not records:
        click.echo(f"No eval runs found in experiment {effective_experiment!r}.")
        click.echo("Run eval first:  apx-agent eval run EVALSET.jsonl --model <endpoint>")
        return

    rows: list[dict[str, Any]] = []
    for rec in records:
        run_id = rec.get("run_id") or "-"
        start_ts = rec.get("start_time")
        start_str = str(start_ts)[:19] if start_ts else "-"
        metrics: dict[str, Any] = {}
        for candidate in _METRIC_CANDIDATES:
            col = f"metrics.{candidate}"
            val = rec.get(col)
            if val is not None:
                metrics[candidate] = round(float(val), 4)
        rows.append({"run_id": run_id, "start_time": start_str, "metrics": metrics})

    if fmt == "json":
        click.echo(json.dumps(rows, indent=2, default=str))
        return

    # Discover which metric columns are non-empty.
    present_metrics: list[str] = [
        m for m in _METRIC_CANDIDATES
        if any(r["metrics"].get(m) is not None for r in rows)
    ]

    header = f"{'RUN_ID':<32}  {'START_TIME':<20}"
    for m in present_metrics:
        short = m.split("/")[-1][:16]
        header += f"  {short:>16}"
    click.echo(f"# eval report: {effective_experiment} (last {len(rows)} runs)")
    click.echo(header)
    for r in rows:
        line = f"{r['run_id'][:32]:<32}  {r['start_time']:<20}"
        for m in present_metrics:
            v = r["metrics"].get(m)
            line += f"  {(str(round(v, 4)) if v is not None else '-'):>16}"
        click.echo(line)

    if not present_metrics:
        click.echo(
            "\n(No standard eval metrics found. "
            "Run apx-agent eval run ... to populate metrics.)"
        )


# ---------------------------------------------------------------------------
# canary — multi-version traffic split helpers
# ---------------------------------------------------------------------------


def _apps_deploy_prod_at_commit(
    *,
    cwd: Path,
    uc_name: str,
    prod_app_name: str,
    profile: str | None,
    prod_target: str,
    expected_sha: str,
    prev_prod_version: str | None,
    as_json: bool = False,
    log: Any,
) -> str | None:
    """Shared gate-don't-mutate prod deploy for ``promote`` and ``rollback``.

    Verifies the working tree is at ``expected_sha`` and clean (never checks
    out), deploys prod via the shared faithful path (readyz is the gate-OUT,
    register_uc mints a new prod manifest version tagged role=prod + the SHA),
    then points the ``@prod`` alias at the new version. Returns the new version,
    or None if it couldn't be resolved. Raises ``click.ClickException`` on a
    gate failure or a failed/unhealthy prod deploy (alias left untouched).

    ``as_json`` routes the nested deploy's stdout (its final app-URL echo) to
    stderr so the caller's ``--json`` contract — exactly one JSON object on
    stdout — holds (issue #417).
    """
    from apx_agent import get_latest_prod_version, set_prod_alias_version

    head = _git_head_sha(cwd)
    if head is None:
        raise click.ClickException(
            "cwd is not a git repo; cannot verify the commit before deploy."
        )
    if head != expected_sha:
        raise click.ClickException(
            f"Your working tree is at {head[:12]}, but the target commit is "
            f"{expected_sha[:12]}. To ship exactly it:\n"
            f"    git checkout {expected_sha}\n"
            "then re-run. (this never changes your working tree.)"
        )
    if _git_is_dirty(cwd):
        raise click.ClickException(
            "Working tree has uncommitted changes — prod would not ship exactly "
            f"{expected_sha[:12]}. Commit or stash them, then re-run."
        )

    try:
        with (contextlib.redirect_stdout(sys.stderr) if as_json
              else contextlib.nullcontext()):
            _deploy_apps_impl(
                cwd=cwd, module="agent:agent", profile=profile,
                bundle_target=prod_target, no_run=False, auto_update_yml=False,
                auto_build_wheel=True, auto_experiment=True, vars=(),
                json_output=False, readyz_gate=True, register_uc=True, uc_name=uc_name,
                app_name_override=prod_app_name,
                extra_version_tags={"apx.apps.role": "prod", "apx.apps.git_sha": expected_sha},
                log=log,
            )
    except Exception as e:
        hint = (
            f" Last good prod version was {prev_prod_version}; to recover: "
            "git checkout <its commit> && apx canary rollback --target apps "
            "--to-version <prev>." if prev_prod_version else ""
        )
        raise click.ClickException(
            f"prod deploy/readyz gate failed: {type(e).__name__}: {e}. The @prod "
            f"alias was NOT moved.{hint}"
        ) from e

    # Only consider prod-tagged versions, so a swallowed manifest registration
    # can never point @prod at the canary version (get_latest_apps_version took
    # the max over ALL roles). Compare against the prior prod version to avoid
    # claiming a move when no new prod manifest was actually registered.
    new_version = get_latest_prod_version(uc_name)
    if new_version and new_version != prev_prod_version:
        try:
            set_prod_alias_version(uc_name, new_version)
            log(f"# @prod → version {new_version}")
        except Exception as e:
            log(f"# warning: couldn't move @prod alias (non-fatal): {e}")
    elif new_version:
        log(
            f"# @prod unchanged at version {new_version} — no new prod manifest "
            f"registered this deploy (app is live, but not version-tracked)."
        )
    else:
        log(
            "# warning: no prod manifest version found — @prod not moved "
            "(app is live, but not version-tracked)."
        )
    return new_version


@main.group(cls=_ApxGroup)
def fleet() -> None:
    """Workspace-scoped bulk operations across many apx-agents.

    Select a set of agents (by --catalog/--schema scope, --name glob,
    --where tag predicates, or explicit --uc-name) and act on them in bulk.
    Mutating commands are dry-run by default; pass --apply to execute.
    """


@fleet.command("list")
@_fleet_select_options
@click.option("--format", "fmt", type=click.Choice(["text", "json"]),
              default="text", help="Output format.")
def fleet_list_cmd(
    catalog: str | None,
    schema: str | None,
    name_glob: str | None,
    where_exprs: tuple[str, ...],
    uc_names: tuple[str, ...],
    profile: str | None,
    fmt: str,
) -> None:
    """Resolve a selection and print the matching agents (read-only)."""
    if schema and not catalog:
        raise click.UsageError("--schema requires --catalog.")
    ws = _require_sdk(profile)
    agents_ = _fleet_resolve(
        ws, catalog=catalog, schema=schema, name_glob=name_glob,
        where_exprs=where_exprs, uc_names=uc_names,
    )
    if fmt == "json":
        click.echo(json.dumps([
            {"agent_name": a.name, "uc_name": a.uc_name, "model": a.model,
             "app_name": a.app_name, "labels": a.labels}
            for a in agents_
        ], indent=2, default=str))
        return
    if not agents_:
        click.echo("No agents matched the selection.")
        return
    click.echo(f"{'AGENT':<24}  {'UC NAME':<40}  {'APP':<22}  LABELS")
    for a in agents_:
        labels = ",".join(f"{k}={v}" for k, v in sorted(a.labels.items())) or "-"
        click.echo(f"{(a.name or '-'):<24}  {a.uc_name:<40}  "
                   f"{(a.app_name or '-'):<22}  {labels}")


@fleet.command("tag")
@_fleet_select_options
@click.option("--set", "set_pairs", multiple=True,
              help="Label to set: key=value (repeatable). Writes apx.label.<key>.")
@click.option("--remove", "remove_keys", multiple=True,
              help="Label key to remove (repeatable).")
@click.option("--apply", is_flag=True, help="Execute. Without it, dry-run.")
def fleet_tag_cmd(
    catalog: str | None,
    schema: str | None,
    name_glob: str | None,
    where_exprs: tuple[str, ...],
    uc_names: tuple[str, ...],
    profile: str | None,
    set_pairs: tuple[str, ...],
    remove_keys: tuple[str, ...],
    apply: bool,
) -> None:
    """Set or remove user labels (apx.label.*) across the selection."""
    from apx_agent import _fleet

    if schema and not catalog:
        raise click.UsageError("--schema requires --catalog.")
    if not set_pairs and not remove_keys:
        raise click.UsageError("Pass at least one --set or --remove.")
    try:
        sets = _fleet.parse_where(list(set_pairs))
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    # Reject reserved namespaces before touching the workspace.
    for key in list(sets) + list(remove_keys):
        if _fleet.is_reserved(key):
            raise click.UsageError(
                f"Refusing to modify reserved system tag '{key}'. "
                "fleet tag only writes user labels (apx.label.*)."
            )

    ws = _require_sdk(profile)
    agents_ = _fleet_resolve(
        ws, catalog=catalog, schema=schema, name_glob=name_glob,
        where_exprs=where_exprs, uc_names=uc_names,
    )
    if not agents_:
        click.echo("No agents matched the selection.")
        return

    from mlflow.tracking import MlflowClient
    client = MlflowClient() if apply else None

    outcomes: list[_fleet.AgentOutcome] = []
    for a in agents_:
        changes = [f"+{_fleet.to_label_key(k)}={v}" for k, v in sets.items()]
        changes += [f"-{_fleet.to_label_key(k)}" for k in remove_keys]
        try:
            if apply:
                assert client is not None
                for k, v in sets.items():
                    client.set_registered_model_tag(a.uc_name, _fleet.to_label_key(k), v)
                for k in remove_keys:
                    client.delete_registered_model_tag(a.uc_name, _fleet.to_label_key(k))
            outcomes.append(_fleet.AgentOutcome(a.uc_name, "ok", " ".join(changes)))
        except Exception as e:  # continue + report
            outcomes.append(_fleet.AgentOutcome(a.uc_name, "failed", str(e)))

    text, code = _fleet.render_summary(outcomes, apply=apply)
    click.echo(text)
    if code:
        raise SystemExit(code)


@fleet.command("backfill")
@click.option("--uc-name", "uc_names", multiple=True, required=True,
              help="Registered-model name to backfill (repeatable, required).")
@click.option("--name", "agent_name", default=None,
              help="apx.agent.name to stamp. Defaults to the model name.")
@click.option("--app", "app_name", default=None,
              help="Workspace App name to stamp as apx.apps.app_name.")
@click.option("--apply", is_flag=True, help="Execute. Without it, dry-run.")
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks config profile.")
def fleet_backfill_cmd(
    uc_names: tuple[str, ...],
    agent_name: str | None,
    app_name: str | None,
    apply: bool,
    profile: str | None,
) -> None:
    """Stamp missing identity/discovery tags onto agents that predate tagging.

    Partial by design: stamps apx.agent.name, apx.serving, and (with --app)
    apx.apps.app_name. It cannot reconstruct apx.agent.tools/resources/metadata.
    """
    from apx_agent import _fleet

    from mlflow.tracking import MlflowClient
    client = MlflowClient()

    outcomes: list[_fleet.AgentOutcome] = []
    for uc in uc_names:
        try:
            existing = {t.key: t.value
                        for t in (getattr(client.get_registered_model(uc), "tags", None) or [])}
            want = {
                _fleet.NAME_TAG: agent_name or uc.split(".")[-1],
                "apx.serving": "apps",
            }
            if app_name:
                want[_fleet.APP_NAME_TAG] = app_name
            missing = {k: v for k, v in want.items() if k not in existing}
            if not missing:
                outcomes.append(_fleet.AgentOutcome(uc, "skipped", "already tagged"))
                continue
            if apply:
                for k, v in missing.items():
                    client.set_registered_model_tag(uc, k, v)
            detail = "stamp " + ",".join(sorted(missing))
            outcomes.append(_fleet.AgentOutcome(uc, "ok", detail))
        except Exception as e:
            outcomes.append(_fleet.AgentOutcome(uc, "failed", str(e)))

    text, code = _fleet.render_summary(outcomes, apply=apply)
    click.echo(text)
    if not apply:
        click.echo("Note: backfill cannot reconstruct tools/resources/metadata.")
    if code:
        raise SystemExit(code)


def _do_fleet_repoint(
    catalog: str | None,
    schema: str | None,
    name_glob: str | None,
    where_exprs: tuple[str, ...],
    uc_names: tuple[str, ...],
    profile: str | None,
    apply: bool,
    fail_fast: bool,
) -> None:
    """Shared by `fleet repoint` and the deprecated `fleet redeploy` alias."""
    from apx_agent import _apps_registry, _fleet

    if schema and not catalog:
        raise click.UsageError("--schema requires --catalog.")
    ws = _require_sdk(profile)
    agents_ = _fleet_resolve(
        ws, catalog=catalog, schema=schema, name_glob=name_glob,
        where_exprs=where_exprs, uc_names=uc_names,
    )
    if not agents_:
        click.echo("No agents matched the selection.")
        return

    outcomes: list[_fleet.AgentOutcome] = []
    for a in agents_:
        try:
            latest = _apps_registry.get_latest_prod_version(a.uc_name)
            current = _apps_registry.get_prod_alias_version(a.uc_name)
            if latest is None:
                outcomes.append(_fleet.AgentOutcome(
                    a.uc_name, "skipped",
                    "no prod-tagged versions (model-serving or never prod-deployed)"))
                continue
            if latest == current:
                outcomes.append(_fleet.AgentOutcome(
                    a.uc_name, "skipped", f"already @{latest}"))
                continue
            if apply:
                _apps_registry.set_prod_alias_version(a.uc_name, latest)
            outcomes.append(_fleet.AgentOutcome(
                a.uc_name, "ok", f"{current or '-'} -> {latest}"))
        except Exception as e:
            outcomes.append(_fleet.AgentOutcome(a.uc_name, "failed", str(e)))
            if fail_fast:
                break

    text, code = _fleet.render_summary(outcomes, apply=apply)
    click.echo(text)
    if code:
        raise SystemExit(code)


@fleet.command("repoint")
@_fleet_select_options
@click.option("--apply", is_flag=True, help="Execute. Without it, dry-run.")
@click.option("--fail-fast", is_flag=True, help="Stop at the first failure.")
def fleet_repoint_cmd(
    catalog: str | None,
    schema: str | None,
    name_glob: str | None,
    where_exprs: tuple[str, ...],
    uc_names: tuple[str, ...],
    profile: str | None,
    apply: bool,
    fail_fast: bool,
) -> None:
    """Move each selected agent's @prod UC alias to its latest prod version.

    This moves a pointer, nothing more: no wheel build, no bundle deploy, no
    app restart. Fleet has no access to per-agent source checkouts, so it
    cannot rebuild — use `apx-agent agents deploy` from an agent's project for
    an actual redeploy.

    "Latest" means the highest version carrying a prod manifest tag — never a
    canary. Mirrors the single-agent prod-promote path (see
    _apps_registry.get_latest_prod_version). Applies only to apps-target
    agents with prod-tagged versions; model-serving agents and agents never
    prod-deployed are reported as skipped with the reason.
    """
    _do_fleet_repoint(
        catalog=catalog, schema=schema, name_glob=name_glob,
        where_exprs=where_exprs, uc_names=uc_names, profile=profile,
        apply=apply, fail_fast=fail_fast,
    )


@fleet.command("redeploy", hidden=True)
@_fleet_select_options
@click.option("--apply", is_flag=True, help="Execute. Without it, dry-run.")
@click.option("--fail-fast", is_flag=True, help="Stop at the first failure.")
def fleet_redeploy_cmd(
    catalog: str | None,
    schema: str | None,
    name_glob: str | None,
    where_exprs: tuple[str, ...],
    uc_names: tuple[str, ...],
    profile: str | None,
    apply: bool,
    fail_fast: bool,
) -> None:
    """DEPRECATED — renamed to ``fleet repoint``. This alias still works."""
    click.echo(
        "# `fleet redeploy` is renamed to `fleet repoint` — this only moves the "
        "@prod alias, it does not rebuild or redeploy.",
        err=True,
    )
    _do_fleet_repoint(
        catalog=catalog, schema=schema, name_glob=name_glob,
        where_exprs=where_exprs, uc_names=uc_names, profile=profile,
        apply=apply, fail_fast=fail_fast,
    )


# ---------------------------------------------------------------------------
# label — judge-alignment labeling sessions
# ---------------------------------------------------------------------------

@main.group(cls=_ApxGroup)
def label() -> None:
    """Judge-alignment: create SME labeling sessions and align judges."""


def _label_one_agent(profile: str | None, **selectors: Any) -> Any:
    """Resolve exactly one apx agent or raise a UsageError."""
    ws = _require_sdk(profile)
    agents = _fleet_resolve(ws, **selectors)
    if len(agents) != 1:
        raise click.UsageError(
            f"selector resolved {len(agents)} agents; narrow it so exactly one matches "
            f"(use --uc-name catalog.schema.model)."
        )
    return agents[0]


def _label_mlflow_setup(profile: str | None) -> None:
    """Point MLflow + the Databricks labeling client at the workspace.

    The label commands run client-side; without this MLflow defaults to a
    local ./mlruns store and never reaches the workspace. We pin the config
    profile (so the databricks-agents review-app client authenticates to the
    same workspace the --profile flag selects) and set the databricks
    tracking URI that the labeling APIs read from ambient context.
    """
    import mlflow

    if profile:
        os.environ["DATABRICKS_CONFIG_PROFILE"] = profile
    mlflow.set_tracking_uri("databricks")


def _resolve_label_agent(
    profile: str | None,
    *,
    agent_name: str | None,
    experiment: str | None,
    need_name: bool,
    **selectors: Any,
) -> Any:
    """Resolve the target agent for a label command.

    Two ways to target an agent:
    - **Fleet selector** (--uc-name / --catalog / ...): resolves exactly one
      UC-registered agent and reads its tags. Works for Model-Serving agents.
    - **Explicit** (no selector + --experiment, plus --agent-name for `start`):
      targets an agent directly without fleet discovery. Required for
      Apps-deployed agents, which have no fleet-discoverable UC registered
      model. Returns a lightweight stand-in carrying just name + empty tags.
    """
    has_selector = any([
        selectors.get("catalog"), selectors.get("schema"),
        selectors.get("name_glob"), selectors.get("where_exprs"),
        selectors.get("uc_names"),
    ])
    if has_selector:
        return _label_one_agent(profile, **selectors)
    # explicit-target path
    if not experiment:
        hint = " with --agent-name <name>" if need_name else ""
        raise click.UsageError(
            f"provide a fleet selector (e.g. --uc-name catalog.schema.model) or "
            f"target the agent directly with --experiment <id>{hint}. "
            f"(Apps-deployed agents have no fleet-discoverable UC model, so use "
            f"the explicit form.)"
        )
    if need_name and not agent_name:
        raise click.UsageError(
            "--experiment without a selector also needs --agent-name <name>."
        )
    from types import SimpleNamespace
    return SimpleNamespace(name=(agent_name or ""), tags={})


@label.command("start")
@_fleet_select_options
@click.option("--judge", "judge_name", required=True, help="Registered judge (scorer) name.")
@click.option("--scale", default=None, help="Numeric judge scale, MIN-MAX (e.g. 1-5).")
@click.option("--options", "options_csv", default=None, help="Categorical judge options, comma-separated.")
@click.option("--experiment", "experiment", default=None, help="MLflow experiment id (overrides the agent tag; required when targeting an agent without a fleet selector).")
@click.option("--agent-name", "agent_name", default=None, help="Target an agent by name without a fleet selector (with --experiment). Use for Apps-deployed agents that aren't fleet-discoverable.")
@click.option("--assignee", "assignees", multiple=True, help="SME email (repeatable). Defaults to you.")
@click.option("--filter", "filter_string", default=None, help="MLflow trace filter_string.")
@click.option("--limit", default=None, type=int, help="Max traces to include.")
@click.option("--endpoint", default=None, help="Serving endpoint for the Review App agent.")
@click.option("--no-review-agent", "attach_agent", flag_value=False, default=True,
              help="Do not attach the agent to the Review App.")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def label_start_cmd(
    catalog: str | None, schema: str | None, name_glob: str | None,
    where_exprs: tuple[str, ...], uc_names: tuple[str, ...],
    profile: str | None,
    judge_name: str, scale: str | None, options_csv: str | None,
    experiment: str | None, agent_name: str | None,
    assignees: tuple[str, ...], filter_string: str | None,
    limit: int | None, endpoint: str | None, attach_agent: bool,
    fmt: str,
) -> None:
    """Create an SME labeling session for a deployed agent's judge."""
    from datetime import datetime, timezone
    from apx_agent import _labeling

    _label_mlflow_setup(profile)
    agent = _resolve_label_agent(
        profile, agent_name=agent_name, experiment=experiment, need_name=True,
        catalog=catalog, schema=schema, name_glob=name_glob,
        where_exprs=where_exprs, uc_names=uc_names,
    )
    try:
        eid = _labeling.resolve_experiment_id(explicit=experiment, agent_tags=agent.tags)
        result = _labeling.start_session(
            experiment_id=eid, agent_name=agent.name, judge_name=judge_name,
            scale=scale, options=(options_csv.split(",") if options_csv else None),
            assignees=list(assignees) or [],
            filter_string=filter_string, limit=limit, endpoint=endpoint,
            attach_agent=attach_agent, now=datetime.now(timezone.utc),
        )
    except _labeling.LabelingError as e:
        raise click.ClickException(str(e)) from e

    if fmt == "json":
        import json as _json
        click.echo(_json.dumps(result.__dict__))
    else:
        click.echo(f"Labeling session: {result.session_url}")
        click.echo(f"  run-id:  {result.run_id}   (pass to `label align --run`)")
        click.echo(f"  traces:  {result.trace_count}   schema/judge: {result.schema_name}")


@label.command("align")
@_fleet_select_options
@click.option("--judge", "judge_name", required=True, help="Registered judge (scorer) name.")
@click.option("--run", "run_id", required=True, help="Run id printed by `label start`.")
@click.option("--experiment", "experiment", default=None, help="MLflow experiment id (overrides the agent tag; pass it with no fleet selector to target an Apps-deployed agent directly).")
@click.option("--reflection-model", default="databricks:/databricks-claude-sonnet-4-6",
              help="Model used by MemAlign to distill guidelines.")
@click.option("--embedding-model", default="databricks:/databricks-gte-large-en",
              help="Embedding model for MemAlign retrieval.")
@click.option("--retrieval-k", default=5, type=int, help="Examples retrieved per evaluation.")
@click.option("--new-version", default=None, help="Register the aligned judge under a new name (preserve the original).")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def label_align_cmd(
    catalog: str | None, schema: str | None, name_glob: str | None,
    where_exprs: tuple[str, ...], uc_names: tuple[str, ...],
    profile: str | None,
    judge_name: str, run_id: str, experiment: str | None,
    reflection_model: str, embedding_model: str, retrieval_k: int,
    new_version: str | None, fmt: str,
) -> None:
    """Align the judge from a finished labeling run (requires apx-agent[align])."""
    from apx_agent import _labeling

    _label_mlflow_setup(profile)
    agent = _resolve_label_agent(
        profile, agent_name=None, experiment=experiment, need_name=False,
        catalog=catalog, schema=schema, name_glob=name_glob,
        where_exprs=where_exprs, uc_names=uc_names,
    )
    try:
        eid = _labeling.resolve_experiment_id(explicit=experiment, agent_tags=agent.tags)
        result = _labeling.align_judge(
            experiment_id=eid, judge_name=judge_name, run_id=run_id,
            reflection_model=reflection_model, embedding_model=embedding_model,
            retrieval_k=retrieval_k, new_version=new_version,
        )
    except _labeling.LabelingError as e:
        raise click.ClickException(str(e)) from e

    if fmt == "json":
        import json as _json
        click.echo(_json.dumps(result.__dict__))
    else:
        click.echo(f"Aligned judge registered as: {result.registered_as}")
        for i, g in enumerate(result.guidelines, 1):
            click.echo(f"  {i}. {g}")


@main.group(cls=_ApxGroup)
def canary() -> None:
    """Canary / A-B deployment helpers — multi-version traffic split."""


@canary.command("status")
@click.option("--target", "deploy_target",
              type=click.Choice(["model-serving", "apps"]),
              default="model-serving",
              help="Deploy target. See docs/apps-canary-hotswap-design.md.")
@click.option("--endpoint", default=None,
              help="Model Serving endpoint name (model-serving target only).")
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks CLI profile (~/.databrickscfg).")
@click.option("--json", "as_json", is_flag=True,
              help="Emit a single JSON result object on stdout "
                   "(ok:false + error on failure).")
def canary_status(
    deploy_target: str, endpoint: str | None, profile: str | None, as_json: bool,
) -> None:
    """Show the live canary/prod state — served entities + traffic split
    (model-serving), or the @prod alias + latest canary version with their
    commits (apps)."""
    with _json_cli_errors(as_json):
        _canary_status_impl(
            deploy_target=deploy_target, endpoint=endpoint,
            profile=profile, as_json=as_json,
        )


def _canary_status_impl(
    *, deploy_target: str, endpoint: str | None, profile: str | None, as_json: bool,
) -> None:
    """Body of ``canary status`` — see the command docstring."""
    if deploy_target == "model-serving":
        if not endpoint:
            raise click.UsageError("--endpoint is required for --target model-serving.")
        from apx_agent import get_canary_config

        ws, _ = _connect_workspace(profile)
        cfg = get_canary_config(endpoint, ws=ws)
        if as_json:
            click.echo(json.dumps({
                "ok": True,
                "target": "model-serving",
                "endpoint": cfg.endpoint,
                "served_entities": [
                    {"name": name, "entity": entity, "version": version,
                     "traffic_pct": cfg.traffic_split.get(name, 0)}
                    for name, entity, version in cfg.served_entities
                ],
            }))
            return
        click.echo(f"# canary status: {cfg.endpoint}")
        if not cfg.served_entities:
            click.echo("  (no served entities)")
            return
        click.echo(f"{'ENTITY':<40}  {'MODEL':<32}  {'VERSION':<10}  {'TRAFFIC %':>9}")
        for name, entity, version in cfg.served_entities:
            pct = cfg.traffic_split.get(name, 0)
            click.echo(f"{name:<40}  {entity:<32}  {version:<10}  {pct:>9}")
        return

    # --target apps — read the UC ledger: which version is @prod, which is the
    # latest canary, and the commit each shipped.
    from apx_agent import (
        find_latest_canary_version,
        get_prod_alias_version,
        get_version_git_sha,
    )

    cwd = Path.cwd()
    doc = _read_databricks_yml(cwd)
    _, base_app_name = _resolve_app_name(doc)
    config = _read_apx_agent_config()
    uc_name = _resolve_apps_uc_name(config, base_app_name)
    if uc_name is None:
        raise click.UsageError(
            "status --target apps needs a UC model name; set "
            "[tool.apx.agent].registered_model (or a non-placeholder "
            "catalog + schema)."
        )

    if not as_json:
        click.echo(f"# canary status (apps): {uc_name}")
    prod_v = get_prod_alias_version(uc_name)
    prod_sha = get_version_git_sha(uc_name, prod_v) if prod_v else None
    canary = find_latest_canary_version(uc_name)

    if as_json:
        click.echo(json.dumps({
            "ok": True,
            "target": "apps",
            "uc_name": uc_name,
            "prod_version": prod_v,
            "prod_git_sha": prod_sha,
            "canary_version": canary.version if canary else None,
            "canary_git_sha": canary.git_sha if canary else None,
        }))
        return

    if prod_v:
        click.echo(f"  @prod  → version {prod_v}  (commit {(prod_sha or '?')[:12]})")
    else:
        click.echo("  @prod  → (unset — nothing promoted yet)")

    if canary:
        click.echo(
            f"  canary → version {canary.version}  "
            f"(commit {(canary.git_sha or '?')[:12]})"
        )
        if canary.git_sha and canary.git_sha != prod_sha:
            click.echo(f"           promote ships {canary.git_sha[:12]} to prod")
    else:
        click.echo("  canary → (none deployed)")


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
@click.option("--json", "as_json", is_flag=True,
              help="Emit a single JSON result object on stdout "
                   "(ok:false + error on failure; progress → stderr).")
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
    as_json: bool,
) -> None:
    """Add a new model version (or App) as a canary."""
    with _json_cli_errors(as_json):
        _canary_deploy_impl(
            deploy_target=deploy_target, endpoint=endpoint,
            registered_model_name=registered_model_name, version=version,
            traffic_pct=traffic_pct, workload_size=workload_size,
            no_scale_to_zero=no_scale_to_zero, canary_version=canary_version,
            profile=profile, base_target=base_target, as_json=as_json,
        )


def _canary_deploy_impl(
    *,
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
    as_json: bool,
) -> None:
    """Body of ``canary deploy`` — see the command docstring."""
    if deploy_target == "model-serving":
        if not endpoint:
            raise click.UsageError("--endpoint is required for --target model-serving.")
        if not registered_model_name:
            raise click.UsageError("--model is required for --target model-serving.")
        if not version:
            raise click.UsageError("--version is required for --target model-serving.")
        from apx_agent import deploy_canary

        ws, _ = _connect_workspace(profile)
        cfg = deploy_canary(
            endpoint=endpoint,
            registered_model_name=registered_model_name,
            new_version=version,
            canary_traffic_pct=traffic_pct,
            ws=ws,
            scale_to_zero_enabled=not no_scale_to_zero,
            workload_size=workload_size,
        )
        if as_json:
            click.echo(json.dumps({
                "ok": True,
                "target": "model-serving",
                "endpoint": endpoint,
                "model": registered_model_name,
                "version": version,
                "traffic_pct": traffic_pct,
                "traffic_split": cfg.traffic_split,
            }))
            return
        click.echo(f"Deployed {registered_model_name} v{version} at {traffic_pct}% on {endpoint}.")
        click.echo(f"New split: {cfg.traffic_split}")
        return

    # --target apps
    if not canary_version:
        raise click.UsageError("--canary-version is required for --target apps.")
    # Flag hygiene (issue #413): Apps has no platform-level traffic split —
    # the value is recorded as a hint only, it routes nothing.
    if (click.get_current_context().get_parameter_source("traffic_pct")
            == click.core.ParameterSource.COMMANDLINE):
        click.echo(
            "# WARNING: --traffic is a recorded hint only for --target apps — "
            "Databricks Apps has no platform-level traffic split; clients hit "
            "whichever App URL they call.",
            err=True,
        )
    from apx_agent import deploy_canary_app

    cwd = Path.cwd()
    doc = _read_databricks_yml(cwd)
    bundle_key, base_app_name = _resolve_app_name(doc)

    def log(msg: str) -> None:
        click.echo(msg, err=True)

    # P1 provenance: capture the commit the canary is deployed from so its UC
    # manifest records it (apx.apps.git_sha). promote verifies prod ships the
    # same commit. Best-effort — None when cwd isn't a git repo.
    git_sha = _git_head_sha(cwd)
    if git_sha:
        log(f"# provenance: canary git SHA {git_sha[:12]}")
    else:
        log("# provenance: no git SHA captured (cwd not a git repo) — "
            "promote won't be able to verify the soaked commit")

    def _deploy_fn(*, bundle_target: str, app_name_override: str,
                   extra_version_tags: dict[str, str]) -> None:
        # Under --json the nested deploy's stdout (its final app-URL echo)
        # routes to stderr so stdout stays a single JSON object (issue #417).
        with (contextlib.redirect_stdout(sys.stderr) if as_json
              else contextlib.nullcontext()):
            _deploy_apps_impl(
                cwd=cwd,
                module="agent:agent",
                profile=profile,
                bundle_target=bundle_target,
                no_run=False,
                auto_update_yml=False,
                auto_build_wheel=True,
                auto_experiment=True,
                vars=(),
                json_output=False,
                readyz_gate=True,
                register_uc=True,
                uc_name=None,
                app_name_override=app_name_override,
                extra_version_tags=extra_version_tags,
                log=log,
            )

    cfg = deploy_canary_app(
        cwd=cwd,
        bundle_key=bundle_key,
        base_app_name=base_app_name,
        canary_version=canary_version,
        traffic_hint=traffic_pct,
        deploy_fn=_deploy_fn,
        base_target=base_target,
        git_sha=git_sha,
    )
    if as_json:
        click.echo(json.dumps({
            "ok": True,
            "target": "apps",
            "canary_app_name": cfg.canary_app_name,
            "prod_app_name": cfg.prod_app_name,
            "bundle_target": cfg.bundle_target,
            "canary_version": cfg.canary_version,
            "git_sha": git_sha,
        }))
        return
    click.echo(f"Canary App deployed: {cfg.canary_app_name}")
    click.echo(f"  prod App:   {cfg.prod_app_name}")
    click.echo("  soak via the full deploy path (validate->build->readyz->UC).")
    return


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
@click.option("--json", "as_json", is_flag=True,
              help="Emit a single JSON result object on stdout "
                   "(ok:false + error on failure; progress → stderr).")
def canary_promote(
    deploy_target: str,
    endpoint: str | None,
    registered_model_name: str | None,
    version: str | None,
    canary_version: str | None,
    profile: str | None,
    prod_target: str,
    keep_canary: bool,
    as_json: bool,
) -> None:
    """Send 100% of traffic to a version (model-serving) or re-deploy prod
    off the canary tree (apps).
    """
    with _json_cli_errors(as_json):
        _canary_promote_impl(
            deploy_target=deploy_target, endpoint=endpoint,
            registered_model_name=registered_model_name, version=version,
            canary_version=canary_version, profile=profile,
            prod_target=prod_target, keep_canary=keep_canary, as_json=as_json,
        )


def _canary_promote_impl(
    *,
    deploy_target: str,
    endpoint: str | None,
    registered_model_name: str | None,
    version: str | None,
    canary_version: str | None,
    profile: str | None,
    prod_target: str,
    keep_canary: bool,
    as_json: bool,
) -> None:
    """Body of ``canary promote`` — see the command docstring."""
    if deploy_target == "model-serving":
        if not endpoint or not registered_model_name or not version:
            raise click.UsageError(
                "--endpoint, --model, and --version are required for "
                "--target model-serving."
            )
        from apx_agent import promote_canary

        ws, _ = _connect_workspace(profile)
        cfg = promote_canary(
            endpoint=endpoint,
            registered_model_name=registered_model_name,
            version=version,
            ws=ws,
        )
        if as_json:
            click.echo(json.dumps({
                "ok": True,
                "target": "model-serving",
                "endpoint": endpoint,
                "model": registered_model_name,
                "version": version,
                "traffic_split": cfg.traffic_split,
            }))
            return
        click.echo(f"Promoted {registered_model_name} v{version} to 100% on {endpoint}.")
        click.echo(f"Split: {cfg.traffic_split}")
        return

    # --target apps — gate-don't-mutate promote (see
    # docs/superpowers/specs/2026-06-12-apps-soak-promote-design.md, P2).
    if not canary_version:
        raise click.UsageError(
            "--canary-version is required for --target apps (the deploy-time "
            "label, used to tear down the canary App after promote)."
        )
    from apx_agent import find_latest_canary_version, get_prod_alias_version
    from apx_agent._canary_apps import _teardown_canary

    cwd = Path.cwd()
    doc = _read_databricks_yml(cwd)
    _, base_app_name = _resolve_app_name(doc)
    config = _read_apx_agent_config()
    uc_name = _resolve_apps_uc_name(config, base_app_name)
    if uc_name is None:
        raise click.UsageError(
            "promote needs the canary's UC manifest, but no UC model name "
            "resolves. Set [tool.apx.agent].registered_model (or a "
            "non-placeholder catalog + schema)."
        )

    # 1. Resolve the canary manifest + the commit it soaked.
    canary = find_latest_canary_version(uc_name)
    if canary is None:
        raise click.ClickException(
            f"No canary manifest found for {uc_name}. Deploy a canary first: "
            "apx canary deploy --target apps --canary-version <v>."
        )
    if not canary.git_sha:
        raise click.ClickException(
            "The canary has no recorded git SHA (deployed from a non-git tree), "
            "so promote can't verify prod ships the soaked commit. Re-deploy the "
            "canary from a git checkout."
        )

    # 2-5. Gate-IN (verify HEAD == soaked SHA + clean) → deploy prod via the
    #      shared faithful path (readyz gate-OUT + register_uc) → move @prod.
    def log(msg: str) -> None:
        click.echo(msg, err=True)

    prev_prod_version = get_prod_alias_version(uc_name)
    new_version = _apps_deploy_prod_at_commit(
        cwd=cwd, uc_name=uc_name, prod_app_name=base_app_name, profile=profile,
        prod_target=prod_target, expected_sha=canary.git_sha,
        prev_prod_version=prev_prod_version, as_json=as_json, log=log,
    )

    # 6. Teardown the canary (unless --keep-canary).
    removed = False
    if not keep_canary:
        try:
            removed = _teardown_canary(
                cwd=cwd, canary_version=canary_version,
                run_cmd=_run_databricks_cmd, profile=profile,
                base_app_name=base_app_name,
            )
        except Exception as e:
            click.echo(f"# warning: canary teardown failed (non-fatal): {e}", err=True)

    if as_json:
        click.echo(json.dumps({
            "ok": True,
            "target": "apps",
            "uc_name": uc_name,
            "app_name": base_app_name,
            "git_sha": canary.git_sha,
            "canary_version": canary_version,
            "prod_version": new_version,
            "previous_prod_version": prev_prod_version,
            "canary_removed": removed,
        }))
        return
    click.echo(f"Promoted canary (commit {canary.git_sha[:12]}) → prod App {base_app_name}.")
    click.echo(f"  canary target removed: {removed}")


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
@click.option("--to-version", "to_version", default=None,
              help="UC manifest version to restore prod to (apps target only). "
                   "Its recorded apx.apps.git_sha is the commit prod will ship.")
@click.option("--profile", default=None, help="Databricks CLI profile (apps target only).")
@click.option("--prod-target", default="prod",
              help="Prod DAB target name (apps target only). Default 'prod'.")
@click.option("--json", "as_json", is_flag=True,
              help="Emit a single JSON result object on stdout "
                   "(ok:false + error on failure; progress → stderr).")
def canary_rollback(
    deploy_target: str,
    endpoint: str | None,
    registered_model_name: str | None,
    version: str | None,
    to_version: str | None,
    profile: str | None,
    prod_target: str,
    as_json: bool,
) -> None:
    """Roll back to a prior version. Gate-don't-mutate for apps (verifies your
    tree is at the target version's commit before re-deploying prod)."""
    with _json_cli_errors(as_json):
        _canary_rollback_impl(
            deploy_target=deploy_target, endpoint=endpoint,
            registered_model_name=registered_model_name, version=version,
            to_version=to_version, profile=profile, prod_target=prod_target,
            as_json=as_json,
        )


def _canary_rollback_impl(
    *,
    deploy_target: str,
    endpoint: str | None,
    registered_model_name: str | None,
    version: str | None,
    to_version: str | None,
    profile: str | None,
    prod_target: str,
    as_json: bool,
) -> None:
    """Body of ``canary rollback`` — see the command docstring."""
    if deploy_target == "model-serving":
        if not endpoint or not registered_model_name or not version:
            raise click.UsageError(
                "--endpoint, --model, and --version are required for "
                "--target model-serving."
            )
        from apx_agent import rollback_canary

        ws, _ = _connect_workspace(profile)
        cfg = rollback_canary(
            endpoint=endpoint,
            registered_model_name=registered_model_name,
            version=version,
            ws=ws,
        )
        if as_json:
            click.echo(json.dumps({
                "ok": True,
                "target": "model-serving",
                "endpoint": endpoint,
                "model": registered_model_name,
                "version": version,
                "traffic_split": cfg.traffic_split,
            }))
            return
        click.echo(f"Rolled back to {registered_model_name} v{version} on {endpoint}.")
        click.echo(f"Split: {cfg.traffic_split}")
        return

    # --target apps — gate-don't-mutate rollback: re-deploy prod from the commit
    # a recorded UC version shipped. Same safety as promote; no canary involved.
    if not to_version:
        raise click.UsageError(
            "--to-version is required for --target apps. Pass the UC manifest "
            "version to restore prod to (see `apx canary status --target apps` "
            "or the model's versions in Unity Catalog)."
        )
    from apx_agent import get_prod_alias_version, get_version_git_sha

    cwd = Path.cwd()
    doc = _read_databricks_yml(cwd)
    _, base_app_name = _resolve_app_name(doc)
    config = _read_apx_agent_config()
    uc_name = _resolve_apps_uc_name(config, base_app_name)
    if uc_name is None:
        raise click.UsageError(
            "rollback needs the UC manifest, but no UC model name resolves. "
            "Set [tool.apx.agent].registered_model (or a non-placeholder "
            "catalog + schema)."
        )

    target_sha = get_version_git_sha(uc_name, to_version)
    if not target_sha:
        raise click.ClickException(
            f"Version {to_version} of {uc_name} has no recorded apx.apps.git_sha, "
            "so rollback can't verify the commit. Pick a version deployed with "
            "provenance (P1+)."
        )

    def log(msg: str) -> None:
        click.echo(msg, err=True)

    prev_prod_version = get_prod_alias_version(uc_name)
    new_version = _apps_deploy_prod_at_commit(
        cwd=cwd, uc_name=uc_name, prod_app_name=base_app_name, profile=profile,
        prod_target=prod_target, expected_sha=target_sha,
        prev_prod_version=prev_prod_version, as_json=as_json, log=log,
    )
    if as_json:
        click.echo(json.dumps({
            "ok": True,
            "target": "apps",
            "uc_name": uc_name,
            "app_name": base_app_name,
            "to_version": to_version,
            "git_sha": target_sha,
            "prod_version": new_version,
            "previous_prod_version": prev_prod_version,
        }))
        return
    click.echo(
        f"Rolled back prod App {base_app_name} to the commit of version "
        f"{to_version} (commit {target_sha[:12]})."
    )


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
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks CLI profile (~/.databrickscfg).")
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
    profile: str | None,
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
        from apx_agent import analyze_canary

        ws, _ = _connect_workspace(profile)
        report = analyze_canary(
            endpoint=endpoint,
            experiment=effective_experiment,
            ws=ws,
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
            "or the `apx.git_sha` / `apx.app.name` tags aren't on emitted "
            "traces (version tags require a #404+ re-deploy) — "
            "fall back to `databricks apps logs <name>` for unstructured logs."
        )
        return
    click.echo(f"{'APP / VERSION':<40}  {'REQUESTS':>9}  {'ERRORS':>7}  "
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


@main.group(cls=_ApxGroup)
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
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks CLI profile (~/.databrickscfg).")
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
    profile: str | None,
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

    from apx_agent import run_sql

    where_parts: list[str] = [
        f"ts > CURRENT_TIMESTAMP - INTERVAL {hours} HOUR",
    ]
    if agent_name:
        from ._sql import sql_escape
        where_parts.append(f"agent_name = '{sql_escape(agent_name)}'")
    sql = (
        f"SELECT ts, agent_name, operation, action, reason, "
        f"  policy_id, domain, context, metadata "
        f"FROM {quoted_table} "
        f"WHERE {' AND '.join(where_parts)} "
        f"ORDER BY ts DESC "
        f"LIMIT {limit}"
    )

    ws, _ = _connect_workspace(profile)
    try:
        rows = run_sql(ws, sql, warehouse_id=warehouse_id)
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
    module_spec: str | None,
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


@main.group(cls=_ApxGroup)
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


@memory.command("provision")
@click.option("--store", "store_name", required=True,
              help="UC memory store name: catalog.schema.name")
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks CLI profile.")
@click.option("--description", default="apx-agent managed agent memory",
              help="Description recorded on the memory store.")
def memory_provision_cmd(
    store_name: str,
    profile: str | None,
    description: str,
) -> None:
    """Create a Databricks Managed Agent Memory store (Unity Catalog).

    Run once (per store) before deploying an agent configured with
    ``memory='managed'`` or ``[tool.apx.agent.memory] type='managed'``. The
    served app principal cannot create the store itself, so this is an
    admin / deploy-time step. Idempotent — safe to re-run.
    """
    from databricks.sdk import WorkspaceClient  # noqa: PLC0415

    from ._memory_managed import provision_managed_memory  # noqa: PLC0415

    ws = WorkspaceClient(profile=profile) if profile else WorkspaceClient()
    click.echo(provision_managed_memory(ws.api_client, store_name, description=description))


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
            click.echo(f"  Check memories exist:  apx-agent memory list --principal-id {principal_id!r}")
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
        raise click.ClickException(
            f"No memory with id {memory_id!r}. "
            "Run `apx-agent memory list` to see available IDs."
        )
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
            click.echo(f"  Add one with:  apx-agent memory remember --principal-id {principal_id!r} --content '...'")
            return
        click.echo(f"# memory list for principal={principal_id!r} ({len(rows)} rows)")
        for m in rows:
            click.echo(f"- ({m['id']}) ns={m['namespace']} tags={m['tags']} "
                       f"importance={m['importance']:.2f}  {m['content']}")

    _emit(payload, fmt, text_fn=_text)


@memory.command("export")
@click.option("--principal-id", required=True, help="Export memories for this principal.")
@click.option("--output", "output_file", required=True,
              help="Destination path for the JSONL file. Use '-' to write to stdout.")
@click.option("--namespace", default=None, help="Optional namespace filter.")
@click.option("--tags", "tags_csv", default=None, help="Comma-separated tag filter.")
@click.option("--limit", default=10000, type=int,
              help="Max memories to export. Default 10000.")
@click.option("--store-module", "store_module", default=None,
              help="MODULE:VAR pointing at a MemoryStore instance. "
                   "Falls back to [tool.apx.agent].memory_store.")
def memory_export_cmd(
    principal_id: str,
    output_file: str,
    namespace: str | None,
    tags_csv: str | None,
    limit: int,
    store_module: str | None,
) -> None:
    """Export memories for PRINCIPAL_ID to a JSONL file (one JSON object per line).

    Use ``--output -`` to stream to stdout. The export includes all fields
    returned by ``memory list`` — id, content, namespace, tags, importance,
    metadata, and timestamps. Embeddings are excluded.

    Example — export to file then import into a new store:

    \b
      apx-agent memory export --principal-id alice --output alice.jsonl
    """
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
    lines = [json.dumps(_memory_to_dict(m), default=str) for m in rows]

    if output_file == "-":
        for line in lines:
            click.echo(line)
    else:
        Path(output_file).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        click.echo(f"Exported {len(lines)} memories to {output_file}.")


# --- examples group --------------------------------------------------------


@main.group(cls=_ApxGroup)
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
            click.echo(f"  Check examples exist:  apx-agent examples list --agent-id {agent_id!r}")
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
        raise click.ClickException(
            f"No example with id {example_id!r}. "
            "Run `apx-agent examples list` to see available IDs."
        )
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
            click.echo(f"  Add one with:  apx-agent examples save --agent-id {agent_id!r} --input '...' --output '...'")
            return
        click.echo(f"# examples list for agent={agent_id!r} ({len(rows)} rows)")
        for ex in rows:
            score = f"{ex['score']:.2f}" if ex["score"] is not None else "-"
            click.echo(f"- ({ex['id']}) intent={ex['intent']} score={score} "
                       f"tags={ex['tags']}")
            click.echo(f"    in:  {ex['input']}")
            click.echo(f"    out: {ex['output']}")

    _emit(payload, fmt, text_fn=_text)


@examples.command("import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--agent-id", required=True, help="Agent ID to assign to each imported example.")
@click.option("--store-module", "store_module", default=None,
              help="MODULE:VAR pointing at an ExampleStore instance. "
                   "Falls back to [tool.apx.agent].example_store.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Parse and count rows without writing to the store.")
@click.option(
    "--format", "fmt", type=click.Choice(["json", "text"]),
    default="text", help="Output format. Default: text.",
)
def examples_import_cmd(
    file: str,
    agent_id: str,
    store_module: str | None,
    dry_run: bool,
    fmt: str,
) -> None:
    """Bulk-load examples from a CSV or JSONL file into the ExampleStore.

    FILE may be:

    \b
      • A .jsonl file — one JSON object per line with keys: input, output,
        and optionally: intent, score, tags (list), metadata (dict).
      • A .csv file — column headers must include ``input`` and ``output``;
        optional columns: ``intent``, ``score``, ``tags`` (comma-separated).

    All rows are assigned to AGENT_ID. Existing examples are not replaced.
    Use --dry-run to validate the file without writing.
    """
    import csv as _csv

    path = Path(file)
    suffix = path.suffix.lower()

    raw_rows: list[dict[str, Any]] = []
    if suffix == ".jsonl":
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw_rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise click.ClickException(f"Line {i}: invalid JSON — {exc}") from exc
    elif suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as fh:
            reader = _csv.DictReader(fh)
            for i, row in enumerate(reader, 2):
                raw_rows.append(dict(row))
    else:
        raise click.UsageError(
            f"Unsupported file type: {suffix!r}. Pass a .jsonl or .csv file."
        )

    if not raw_rows:
        click.echo("No rows found in the file. Nothing to import.")
        return

    # Validate required fields.
    for i, row in enumerate(raw_rows, 1):
        if "input" not in row or "output" not in row:
            raise click.ClickException(
                f"Row {i} is missing required field(s): "
                f"{'input' if 'input' not in row else 'output'}. "
                f"Found fields: {list(row.keys())}"
            )

    if dry_run:
        click.echo(f"dry-run: {len(raw_rows)} row(s) would be imported for agent={agent_id!r}.")
        return

    store = _load_store(store_module, store_kind="example")
    imported = 0
    for row in raw_rows:
        payload_in: dict[str, Any] = {
            "agent_id": agent_id,
            "input": row["input"],
            "output": row["output"],
        }
        if row.get("intent"):
            payload_in["intent"] = row["intent"]
        if row.get("score") not in (None, ""):
            try:
                payload_in["score"] = float(row["score"])
            except (ValueError, TypeError):
                pass
        # CSV tags may be a comma-separated string; JSONL may be a list.
        raw_tags = row.get("tags")
        if isinstance(raw_tags, str) and raw_tags.strip():
            payload_in["tags"] = [t.strip() for t in raw_tags.split(",") if t.strip()]
        elif isinstance(raw_tags, list):
            payload_in["tags"] = raw_tags
        if isinstance(row.get("metadata"), dict):
            payload_in["metadata"] = row["metadata"]
        store.add(payload_in)
        imported += 1

    if fmt == "json":
        click.echo(json.dumps({"imported": imported, "agent_id": agent_id}))
    else:
        click.echo(f"Imported {imported} example(s) for agent={agent_id!r}.")


# ---------------------------------------------------------------------------
# memory consolidate — wrap consolidate_memories with injectable stores/callables
# ---------------------------------------------------------------------------
#
# Pyproject fallback keys (under ``[tool.apx.agent]``):
#
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
