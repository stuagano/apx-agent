"""Environment diagnostics for the apx CLI.

The *facts* layer behind `apx-agent doctor` and the inline preflights in cli.py.
Each `check_*` function inspects one thing and returns a `Check`. cli.py owns
presentation; this module owns what's wrong and how to fix it. References to
cli helpers (`_detect_target`, `_databrickscfg_profiles`) are lazy imports so
this module has no import-time dependency on cli (cli imports this module).
"""

from __future__ import annotations

import enum
import importlib
import importlib.metadata
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MIN_PYTHON = (3, 11)

# The internal Databricks PyPI proxy. Unreachable for external users and for
# deployed Apps/serving endpoints, so a uv.lock pinned to it — or an index env
# var pointed at it (which re-poisons the lock on the next `uv sync`) — breaks
# fresh installs and deploys. Mirrors cli._sanitize_uv_lock and the
# scripts/check-uv-lock-registry.sh CI guard.
_PYPI_PROXY_HOST = "pypi-proxy.dev.databricks.com"
_INDEX_ENV_VARS = ("UV_INDEX_URL", "UV_DEFAULT_INDEX", "PIP_INDEX_URL")
_MISSING_ENV_VALUE = ""  # env vars not set compare as empty when checking for proxy host


class Status(enum.Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class Check:
    """One diagnostic result. `fix` is a copy-pasteable next step or None."""

    name: str
    status: Status
    detail: str
    fix: str | None = None


def run_checks(cwd: Path, *, online: bool) -> list[tuple[str, list[Check]]]:
    """Run all checks, grouped and ordered for presentation.

    `online=True` adds the live workspace round-trip; it is skipped when auth
    can't even be resolved (nothing to live-test).
    """
    environment = [
        check_python_version(),
        check_apx_install(),
        check_uv(),
        check_pypi_index(cwd),
        check_databricks_cli(),
        check_uvicorn(),
    ]
    auth = check_databricks_auth()
    authentication = [auth]
    if online:
        authentication.append(
            check_databricks_workspace(auth_ok=auth.status is Status.OK)
        )
        model_check = check_model_endpoint(cwd, auth_ok=auth.status is Status.OK)
        if model_check is not None:
            authentication.append(model_check)
        apps_check = check_apps_enabled(auth_ok=auth.status is Status.OK)
        if apps_check is not None:
            authentication.append(apps_check)
    project = [
        check_project_layout(cwd),
        check_target(cwd),
        check_extras(cwd),
        check_databricks_yml(cwd),
    ]
    memory_check = check_memory_backend(cwd, auth_ok=auth.status is Status.OK)
    if memory_check is not None:
        project.append(memory_check)
    uc_check = check_uc_data_source(cwd, auth_ok=auth.status is Status.OK)
    if uc_check is not None:
        project.append(uc_check)
    project.extend(check_declared_tools(cwd, auth_ok=auth.status is Status.OK))
    return [
        ("Environment", environment),
        ("Authentication", authentication),
        ("Project", project),
    ]


def check_python_version() -> Check:
    major, minor, micro = sys.version_info[:3]
    version = f"{major}.{minor}.{micro}"
    if (major, minor) >= MIN_PYTHON:
        return Check("Python", Status.OK, version, None)
    return Check(
        "Python",
        Status.FAIL,
        f"{version} — apx-agent requires Python >= "
        f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ "
        "(e.g. `uv python install 3.12`) and recreate the venv.",
    )


def check_apx_install() -> Check:
    try:
        version = importlib.metadata.version("apx-agent")
        return Check("apx-agent install", Status.OK, f"version {version}", None)
    except importlib.metadata.PackageNotFoundError:
        return Check(
            "apx-agent install", Status.OK, "dev (editable install)", None
        )


def check_uv() -> Check:
    if shutil.which("uv"):
        return Check("uv", Status.OK, "found", None)
    return Check(
        "uv",
        Status.WARN,
        "not found — used by `uv sync` and the scaffold dev loop",
        "Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh",
    )


def check_pypi_index(cwd: Path) -> Check:
    """Flag the internal Databricks PyPI proxy in uv.lock or an index env var.

    A `uv.lock` pinned to ``pypi-proxy.dev.databricks.com`` fails ``uv sync``
    for external users and deployed Apps (the blocking case). Absent that, an
    index env var pointed at the proxy silently re-records it on the next
    ``uv sync`` (the about-to-break case). See ``cli._sanitize_uv_lock`` and the
    ``scripts/check-uv-lock-registry.sh`` CI guard.
    """
    lock = cwd / "uv.lock"
    try:
        if lock.exists() and _PYPI_PROXY_HOST in lock.read_text():
            return Check(
                "PyPI index",
                Status.FAIL,
                f"uv.lock pins packages to the internal proxy "
                f"({_PYPI_PROXY_HOST}) — unreachable for external users and "
                "deployed Apps, so `uv sync`/deploy will fail off the corp network",
                "Unset the proxy index env var, then `uv lock` to re-resolve "
                "from public PyPI (`apx-agent deploy` also sanitizes the lock before "
                "bundling).",
            )
    except OSError:
        pass

    leaked = next(
        (v for v in _INDEX_ENV_VARS if _PYPI_PROXY_HOST in os.environ.get(v, _MISSING_ENV_VALUE)),
        None,
    )
    if leaked:
        return Check(
            "PyPI index",
            Status.WARN,
            f"{leaked} points at the internal proxy ({_PYPI_PROXY_HOST}) — "
            "`uv sync` will re-poison uv.lock, breaking external installs",
            f"unset {leaked}  (or point it at https://pypi.org/simple).",
        )

    return Check("PyPI index", Status.OK, "public PyPI", None)


def check_databricks_cli() -> Check:
    path = shutil.which("databricks")
    if not path:
        return Check(
            "Databricks CLI",
            Status.WARN,
            "not found — needed for `apx-agent deploy`",
            "brew install databricks/tap/databricks  "
            "(or see docs.databricks.com/dev-tools/cli)",
        )
    try:
        out = subprocess.run(
            ["databricks", "--version"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        detail = (out.stdout or out.stderr or "found").strip().splitlines()[0]
    except (subprocess.SubprocessError, OSError):
        detail = "found"
    return Check("Databricks CLI", Status.OK, detail, None)


def check_uvicorn() -> Check:
    try:
        importlib.import_module("uvicorn")
        return Check("uvicorn", Status.OK, "installed", None)
    except ImportError:
        return Check(
            "uvicorn",
            Status.WARN,
            "not importable — required by `apx-agent run`",
            "uv add 'apx-agent[apps]'  (includes uvicorn[standard])",
        )


def check_databricks_auth() -> Check:
    """Confirm a Databricks Config can be constructed (offline, no API call)."""
    try:
        from databricks.sdk.core import Config
    except Exception as e:  # SDK missing in a minimal install
        return Check(
            "Databricks auth",
            Status.FAIL,
            f"databricks-sdk not importable: {e}",
            "uv add databricks-sdk  (normally pulled in by apx-agent)",
        )
    try:
        Config()
        return Check("Databricks auth", Status.OK, "credentials resolved", None)
    except Exception as e:
        from apx_agent.cli import _databrickscfg_profiles

        profiles = _databrickscfg_profiles()
        if profiles:
            fix = (
                "Pick a profile: DATABRICKS_CONFIG_PROFILE=<name> apx-agent ...  "
                f"(configured: {', '.join(profiles)})"
            )
            detail = "credentials unresolved — profile unset or ambiguous"
        else:
            fix = (
                "databricks auth login --host "
                "https://<your-workspace>.cloud.databricks.com  "
                "(or `databricks configure --token`)"
            )
            detail = "no profiles in ~/.databrickscfg"
        return Check("Databricks auth", Status.FAIL, f"{detail} ({e})", fix)


def check_databricks_workspace(*, auth_ok: bool) -> Check:
    """Live round-trip: confirm the resolved token authenticates (online)."""
    if not auth_ok:
        return Check(
            "Workspace reachable",
            Status.SKIP,
            "skipped — fix Databricks auth first",
            None,
        )
    try:
        from databricks.sdk import WorkspaceClient

        client = WorkspaceClient()
        me = client.current_user.me()
        host = getattr(getattr(client, "config", None), "host", "") or "workspace"
        user = getattr(me, "user_name", None) or getattr(me, "userName", "user")
        return Check(
            "Workspace reachable", Status.OK, f"{user} @ {host}", None
        )
    except Exception as e:
        msg = str(e)
        lower = msg.lower()
        if "401" in msg or "invalid" in lower or "expired" in lower:
            return Check(
                "Workspace reachable",
                Status.FAIL,
                f"token rejected ({msg})",
                "Your token is expired or invalid — re-run "
                "`databricks auth login --host https://<workspace>...`",
            )
        if "403" in msg or "permission" in lower or "denied" in lower:
            return Check(
                "Workspace reachable",
                Status.FAIL,
                f"permission denied ({msg})",
                "Authenticated, but the principal lacks workspace access — "
                "confirm you targeted the right workspace.",
            )
        return Check(
            "Workspace reachable",
            Status.FAIL,
            f"could not reach workspace ({msg})",
            "Check the workspace host URL, your network/VPN, and TLS.",
        )


def check_model_endpoint(cwd: Path, *, auth_ok: bool) -> Check | None:
    """Verify the configured model endpoint exists in the workspace.

    Only runs when inside an apx project (reads `model` from pyproject.toml)
    and auth is available. Returns None when there's nothing to check.
    """
    if not auth_ok:
        return None
    pyproject = cwd / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        data = tomllib.loads(pyproject.read_text())
    except Exception:
        return None
    model: str | None = (
        data.get("tool", {}).get("apx", {}).get("agent", {}).get("model")
    )
    if not model:
        return None
    label = f"Model endpoint ({model})"
    try:
        from databricks.sdk import WorkspaceClient
        ws = WorkspaceClient()
        ep = ws.serving_endpoints.get(model)
        state = getattr(getattr(ep, "state", None), "ready", None)
        if state and str(state).upper() not in ("READY", "NOT_UPDATING"):
            return Check(
                label, Status.WARN,
                f"endpoint exists but state={state}",
                "The model endpoint may be deploying or degraded — try again in a minute.",
            )
        return Check(label, Status.OK, "exists and reachable", None)
    except Exception as e:
        msg = str(e)
        if "404" in msg or "does not exist" in msg.lower() or "not found" in msg.lower():
            return Check(
                label, Status.FAIL,
                f"endpoint not found: {model}",
                "Check the endpoint name under Serving in your workspace. "
                "Update `model` in pyproject.toml [tool.apx.agent] to match.",
            )
        return Check(
            label, Status.WARN,
            f"could not verify endpoint ({msg})",
            "Endpoint lookup failed — check auth and workspace access.",
        )


def _data_source_from_agent_py(cwd: Path) -> "tuple[str, str] | None":
    """Read (catalog, schema) from a top-level ``agent.py`` data-agent call.

    Looks for ``DataAgent("cat", "sch", ...)`` / ``CoworkerAgent("cat", "sch", ...)``
    and returns the first two string args (positional or ``catalog=``/``schema=``).
    AST-based and best-effort — returns None if agent.py is absent or unparseable.
    """
    import ast

    agent_py = cwd / "agent.py"
    if not agent_py.exists():
        return None
    try:
        tree = ast.parse(agent_py.read_text())
    except Exception:
        return None
    targets = {"DataAgent", "CoworkerAgent"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            fname = fn.id
        elif isinstance(fn, ast.Attribute):
            fname = fn.attr
        else:
            continue
        if fname not in targets:
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}

        def _str(v: "ast.expr | None") -> str:
            return v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else ""

        catalog = _str(node.args[0] if node.args else None) or _str(kw.get("catalog"))
        schema = _str(node.args[1] if len(node.args) > 1 else None) or _str(kw.get("schema"))
        if catalog and schema:
            return catalog, schema
    return None


def check_uc_data_source(cwd: Path, *, auth_ok: bool) -> Check | None:
    """Verify the UC catalog.schema declared in [tool.apx.agent.template] exists.

    DataAgent / CoworkerAgent projects declare their data source as:
        [tool.apx.agent.template]
        name = "data"
        catalog = "main"
        schema  = "sales"

    A missing or inaccessible schema is the most common silent failure: the
    agent starts fine, every SQL query 403s, and the user sees "I can't find
    any data" with no clear diagnosis. Returns None when not in an apx project
    or no template catalog/schema is declared.
    """
    if not auth_ok:
        return None
    pyproject = cwd / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        data = tomllib.loads(pyproject.read_text())
    except Exception:
        return None
    tmpl: dict = (
        data.get("tool", {}).get("apx", {}).get("agent", {}).get("template") or {}
    )
    catalog = tmpl.get("catalog", "")
    schema = tmpl.get("schema", "")
    if not catalog or not schema:
        # Python-canonical projects declare the data source in agent.py
        # (DataAgent("cat", "sch") / CoworkerAgent(...)) rather than a
        # [tool.apx.agent.template] section. Fall back to reading it there.
        catalog, schema = _data_source_from_agent_py(cwd) or (catalog, schema)
    if not catalog or not schema:
        return None
    fqn = f"{catalog}.{schema}"
    label = f"UC schema ({fqn})"
    try:
        from databricks.sdk import WorkspaceClient
        ws = WorkspaceClient()
        ws.schemas.get(full_name=fqn)
        return Check(label, Status.OK, "exists and readable", None)
    except Exception as e:
        msg = str(e)
        if "404" in msg or "does not exist" in msg.lower() or "not found" in msg.lower():
            return Check(
                label, Status.FAIL,
                f"schema not found: {fqn}",
                f"Confirm `{fqn}` exists in Unity Catalog and your principal has USE SCHEMA. "
                "Update catalog/schema in pyproject.toml [tool.apx.agent.template] to match.",
            )
        if "403" in msg or "permission" in msg.lower() or "denied" in msg.lower():
            return Check(
                label, Status.FAIL,
                f"permission denied on {fqn}",
                f"Grant USE CATALOG on `{catalog}` and USE SCHEMA on `{fqn}` to your principal.",
            )
        return Check(
            label, Status.WARN,
            f"could not verify schema ({msg})",
            "Schema lookup failed — check auth and Unity Catalog grants.",
        )


def check_apps_enabled(*, auth_ok: bool) -> Check | None:
    """Verify Databricks Apps is enabled in this workspace.

    `apx-agent deploy --target apps` will fail immediately if Apps isn't
    enabled, but nothing warns you until deploy time. This check probes the
    Apps API early so `apx-agent doctor` can catch it before any code is
    bundled or uploaded. Returns None when auth isn't available.
    """
    if not auth_ok:
        return None
    try:
        from databricks.sdk import WorkspaceClient
        ws = WorkspaceClient()
        # Iterate once — lightest possible probe, no results needed.
        # A 404 or FEATURE_DISABLED means Apps is off.
        next(iter(ws.apps.list()), None)
        return Check("Databricks Apps", Status.OK, "enabled in this workspace", None)
    except Exception as e:
        msg = str(e)
        lower = msg.lower()
        if (
            "404" in msg
            or "feature" in lower
            or "disabled" in lower
            or "not enabled" in lower
            or "not available" in lower
        ):
            return Check(
                "Databricks Apps",
                Status.WARN,
                "Apps may not be enabled in this workspace",
                "Ask your workspace admin to enable Databricks Apps, or deploy with "
                "`uv run apx-agent deploy --target model-serving` instead.",
            )
        # Any other error (network, auth) — don't fail the check hard
        return None


def _is_apx_project(cwd: Path) -> bool:
    """True when cwd looks like a scaffolded apx project."""
    pyproject = cwd / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        return "[tool.apx.agent]" in pyproject.read_text()
    except OSError:
        return False


def check_project_layout(cwd: Path) -> Check:
    if not _is_apx_project(cwd):
        return Check(
            "Project layout",
            Status.SKIP,
            f"{cwd} is not an apx project",
            "Run `apx-agent scaffold my-agent` to create one, then cd into it.",
        )
    from apx_agent.cli import _detect_target

    target = _detect_target(cwd)
    marker = "agent_server/" if target == "apps" else "agent.py"
    if target == "apps":
        ok = (cwd / "agent_server").is_dir()
    else:
        ok = (cwd / "agent.py").exists()
    if ok:
        return Check("Project layout", Status.OK, f"{target} layout detected", None)
    return Check(
        "Project layout",
        Status.FAIL,
        f"pyproject declares an agent but {marker} is missing",
        "Re-run `apx-agent scaffold` or restore the agent entrypoint.",
    )


def check_target(cwd: Path) -> Check:
    if not _is_apx_project(cwd):
        return Check("Target", Status.SKIP, "not in an apx project", None)
    from apx_agent.cli import _detect_target

    # _detect_target only ever returns "apps" or "model-serving"; no failure mode to surface.
    return Check("Target", Status.OK, _detect_target(cwd), None)


def check_extras(cwd: Path) -> Check:
    if not _is_apx_project(cwd):
        return Check("Required extra", Status.SKIP, "not in an apx project", None)
    from apx_agent.cli import _detect_target

    target = _detect_target(cwd)
    if target == "apps":
        module, label, fix = (
            "mlflow.genai.agent_server",
            "mlflow.genai (eval extra)",
            "uv add 'apx-agent[eval]'  (or `uv sync --extra eval` in this project)",
        )
    else:
        module, label, fix = (
            "langchain",
            "langchain (required dep)",
            "uv sync  (langgraph/langchain are required deps of apx-agent)",
        )
    try:
        importlib.import_module(module)
        return Check("Required extra", Status.OK, f"{label} installed", None)
    except ImportError:
        return Check(
            "Required extra",
            Status.FAIL,
            f"{label} is not installed (needed for target {target})",
            fix,
        )


def check_databricks_yml(cwd: Path) -> Check:
    if not _is_apx_project(cwd):
        return Check("databricks.yml", Status.SKIP, "not in an apx project", None)
    if (cwd / "databricks.yml").exists():
        return Check("databricks.yml", Status.OK, "present", None)
    return Check(
        "databricks.yml",
        Status.WARN,
        "missing — `apx-agent deploy --target apps` needs it",
        "Re-run `apx-agent scaffold <name> --target apps`, or `apx-agent deploy` "
        "for model-serving (no bundle required).",
    )


def check_memory_backend(cwd: Path, *, auth_ok: bool) -> Check | None:
    """Check that the configured memory backend is reachable.

    Returns ``None`` when no memory config is present (nothing to check).
    Skips the live probe when auth is not available.
    """
    if not _is_apx_project(cwd):
        return None
    try:
        from ._inspection import _load_agent_config  # noqa: PLC0415
        cfg = _load_agent_config()
    except Exception:
        return None
    if cfg is None or cfg.memory is None:
        return None

    mem = cfg.memory
    label = "Memory backend"

    if not auth_ok:
        return Check(label, Status.SKIP, f"type={mem.type} — skipped (auth unavailable)", None)

    if mem.type == "inmemory":
        return Check(label, Status.OK, "inmemory (no external backend)", None)

    if mem.type == "delta":
        if not mem.table_name:
            return Check(
                label, Status.WARN,
                "type=delta but no table_name — memory will degrade at runtime",
                "Add table_name to [tool.apx.agent.memory] and re-run `uv run quickstart`.",
            )
        try:
            from ._defaults import _make_workspace_client  # noqa: PLC0415
            from ._sql import run_sql  # noqa: PLC0415
            ws = _make_workspace_client()
            run_sql(ws, f"SELECT 1 FROM {mem.table_name} LIMIT 0")
            return Check(label, Status.OK, f"delta table reachable: {mem.table_name}", None)
        except Exception as exc:
            short = str(exc)[:120]
            return Check(
                label, Status.WARN,
                f"delta table not yet reachable ({short})",
                "Run `uv run quickstart` to create the memory tables.",
            )

    if mem.type == "lakebase":
        if not mem.instance_name:
            return Check(
                label, Status.WARN,
                "type=lakebase but no instance_name — memory will degrade at runtime",
                "Add instance_name to [tool.apx.agent.memory] and re-run `uv run quickstart`.",
            )
        try:
            from ._defaults import _make_workspace_client  # noqa: PLC0415
            ws = _make_workspace_client()
            ws.database.get_database_instance(mem.instance_name)
            return Check(label, Status.OK, f"lakebase instance exists: {mem.instance_name}", None)
        except Exception as exc:
            short = str(exc)[:120]
            return Check(
                label, Status.WARN,
                f"lakebase instance '{mem.instance_name}' not found ({short})",
                "Run `uv run quickstart` to provision the lakebase instance.",
            )

    return Check(label, Status.SKIP, f"unknown type {mem.type!r}", None)


def check_declared_tools(cwd: Path, *, auth_ok: bool) -> list[Check]:
    """Validate that each declared [[tool.apx.tools]] resource exists in the workspace.

    Covers: Genie spaces, Vector Search indexes, UC functions/toolkits, and SQL
    warehouses (including warehouse_id in [tool.apx.agent.session]).
    """
    if not _is_apx_project(cwd):
        return []

    from apx_agent._tool_config import _read_tools_section  # noqa: PLC0415

    tables = _read_tools_section(str(cwd / "pyproject.toml"))

    # Collect session warehouse_id if configured.
    session_warehouse: str | None = None
    try:
        from ._inspection import _load_agent_config  # noqa: PLC0415

        cfg = _load_agent_config()
        sess = getattr(cfg, "session", None) if cfg else None
        if sess and getattr(sess, "warehouse_id", None):
            session_warehouse = sess.warehouse_id
    except Exception:
        pass

    if not tables and not session_warehouse:
        return []

    checks: list[Check] = []

    _RESOURCE_TYPES = {"genie", "genie_query", "vector_search", "uc_function", "uc_function_toolkit", "sql"}
    resource_tables = [t for t in tables if t.get("type") in _RESOURCE_TYPES]
    if not resource_tables and not session_warehouse:
        return []

    if not auth_ok:
        for table in resource_tables:
            typ = table.get("type", "")
            checks.append(Check(
                f"Tool ({typ})",
                Status.SKIP,
                "skipped — fix Databricks auth first",
                None,
            ))
        if session_warehouse:
            checks.append(Check(
                "Session warehouse",
                Status.SKIP,
                "skipped — fix Databricks auth first",
                None,
            ))
        return checks

    from ._defaults import _make_workspace_client  # noqa: PLC0415

    ws = _make_workspace_client()

    for table in resource_tables:
        typ = table.get("type", "")

        if typ in ("genie", "genie_query"):
            space_id = table.get("space_id", "")
            if not space_id:
                checks.append(Check(
                    "Genie space",
                    Status.WARN,
                    "space_id missing in [[tool.apx.tools]]",
                    "Add space_id to the genie tool config.",
                ))
                continue
            try:
                ws.genie.get_space(space_id=space_id)
                checks.append(Check(f"Genie space ({space_id[:20]})", Status.OK, "found", None))
            except Exception as exc:
                short = str(exc)[:120]
                checks.append(Check(
                    f"Genie space ({space_id[:20]})",
                    Status.WARN,
                    f"not found or not accessible ({short})",
                    f"Confirm space_id {space_id!r} exists and your principal has access.",
                ))

        elif typ == "vector_search":
            index_name = table.get("index_name", "")
            if not index_name:
                checks.append(Check(
                    "VS index",
                    Status.WARN,
                    "index_name missing in [[tool.apx.tools]]",
                    "Add index_name to the vector_search tool config.",
                ))
                continue
            try:
                ws.vector_search_indexes.get_index(index_name=index_name)
                checks.append(Check(f"VS index ({index_name})", Status.OK, "found", None))
            except Exception as exc:
                short = str(exc)[:120]
                checks.append(Check(
                    f"VS index ({index_name})",
                    Status.WARN,
                    f"not found or not accessible ({short})",
                    "Confirm the index exists and has READY status.",
                ))

        elif typ == "uc_function":
            function_name = table.get("function_name", "")
            if not function_name:
                checks.append(Check(
                    "UC function",
                    Status.WARN,
                    "function_name missing in [[tool.apx.tools]]",
                    "Add function_name to the uc_function tool config.",
                ))
                continue
            try:
                ws.functions.get(name=function_name)
                checks.append(Check(f"UC function ({function_name})", Status.OK, "found", None))
            except Exception as exc:
                short = str(exc)[:120]
                checks.append(Check(
                    f"UC function ({function_name})",
                    Status.WARN,
                    f"not found or not accessible ({short})",
                    f"Confirm {function_name!r} exists in Unity Catalog.",
                ))

        elif typ == "uc_function_toolkit":
            catalog_schema = table.get("catalog_schema", "")
            if not catalog_schema:
                checks.append(Check(
                    "UC function toolkit",
                    Status.WARN,
                    "catalog_schema missing in [[tool.apx.tools]]",
                    "Add catalog_schema to the uc_function_toolkit config.",
                ))
                continue
            try:
                ws.schemas.get(full_name=catalog_schema)
                checks.append(Check(f"UC schema ({catalog_schema})", Status.OK, "found", None))
            except Exception as exc:
                short = str(exc)[:120]
                checks.append(Check(
                    f"UC schema ({catalog_schema})",
                    Status.WARN,
                    f"not found or not accessible ({short})",
                    f"Confirm {catalog_schema!r} exists and your principal has USE SCHEMA.",
                ))

        elif typ == "sql":
            warehouse_id = table.get("warehouse_id", "")
            if not warehouse_id:
                continue  # warehouse_id is optional for sql_tool — skip silently
            try:
                ws.warehouses.get(id=warehouse_id)
                checks.append(Check(f"SQL warehouse ({warehouse_id})", Status.OK, "found", None))
            except Exception as exc:
                short = str(exc)[:120]
                checks.append(Check(
                    f"SQL warehouse ({warehouse_id})",
                    Status.WARN,
                    f"not found or not accessible ({short})",
                    f"Confirm warehouse {warehouse_id!r} exists and is running.",
                ))

    if session_warehouse:
        try:
            ws.warehouses.get(id=session_warehouse)
            checks.append(Check(f"Session warehouse ({session_warehouse})", Status.OK, "found", None))
        except Exception as exc:
            short = str(exc)[:120]
            checks.append(Check(
                f"Session warehouse ({session_warehouse})",
                Status.WARN,
                f"not found or not accessible ({short})",
                f"Confirm warehouse {session_warehouse!r} exists and is running.",
            ))

    return checks
