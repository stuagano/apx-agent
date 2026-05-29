"""Environment diagnostics for the apx CLI.

The *facts* layer behind `apx doctor` and the inline preflights in cli.py.
Each `check_*` function inspects one thing and returns a `Check`. cli.py owns
presentation; this module owns what's wrong and how to fix it. References to
cli helpers (`_detect_target`, `_databrickscfg_profiles`) are lazy imports so
this module has no import-time dependency on cli (cli imports this module).
"""

from __future__ import annotations

import enum
import importlib
import importlib.metadata
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MIN_PYTHON = (3, 11)


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
        check_databricks_cli(),
        check_uvicorn(),
    ]
    auth = check_databricks_auth()
    authentication = [auth]
    if online:
        authentication.append(
            check_databricks_workspace(auth_ok=auth.status is Status.OK)
        )
    project = [
        check_project_layout(cwd),
        check_target(cwd),
        check_extras(cwd),
        check_databricks_yml(cwd),
    ]
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


def check_databricks_cli() -> Check:
    path = shutil.which("databricks")
    if not path:
        return Check(
            "Databricks CLI",
            Status.WARN,
            "not found — needed for `apx deploy`",
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
            "not importable — required by `apx run`",
            "pip install 'uvicorn[standard]'  (or `apx-agent[apps]`)",
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
            "pip install databricks-sdk  (normally pulled in by apx-agent)",
        )
    try:
        Config()
        return Check("Databricks auth", Status.OK, "credentials resolved", None)
    except Exception as e:
        from apx_agent.cli import _databrickscfg_profiles

        profiles = _databrickscfg_profiles()
        if profiles:
            fix = (
                "Pick a profile: DATABRICKS_CONFIG_PROFILE=<name> apx ...  "
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
        if "401" in msg or "invalid" in lower or "expired" in lower or "token" in lower:
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
            "Run `apx scaffold my-agent` to create one, then cd into it.",
        )
    from apx_agent.cli import _detect_target

    target = _detect_target(cwd)
    marker = "agent_server/" if target == "apps" else "agent.py"
    if (cwd / "agent_server").is_dir() or (cwd / "agent.py").exists():
        return Check("Project layout", Status.OK, f"{target} layout detected", None)
    return Check(
        "Project layout",
        Status.FAIL,
        f"pyproject declares an agent but {marker} is missing",
        "Re-run `apx scaffold` or restore the agent entrypoint.",
    )


def check_target(cwd: Path) -> Check:
    if not _is_apx_project(cwd):
        return Check("Target", Status.SKIP, "not in an apx project", None)
    from apx_agent.cli import _detect_target

    return Check("Target", Status.OK, _detect_target(cwd), None)


def check_extras(cwd: Path) -> Check:
    if not _is_apx_project(cwd):
        return Check("Required extra", Status.SKIP, "not in an apx project", None)
    from apx_agent.cli import _detect_target

    target = _detect_target(cwd)
    if target == "apps":
        module, extra = "apx_agent._responses_agent", "apps"
    else:
        module, extra = "langchain", "langgraph"
    try:
        importlib.import_module(module)
        return Check("Required extra", Status.OK, f"{extra} extra installed", None)
    except ImportError:
        return Check(
            "Required extra",
            Status.FAIL,
            f"the '{extra}' extra is not installed (needed for target {target})",
            f"pip install 'apx-agent[{extra}]'  (or `uv sync` in this project)",
        )


def check_databricks_yml(cwd: Path) -> Check:
    if not _is_apx_project(cwd):
        return Check("databricks.yml", Status.SKIP, "not in an apx project", None)
    if (cwd / "databricks.yml").exists():
        return Check("databricks.yml", Status.OK, "present", None)
    return Check(
        "databricks.yml",
        Status.WARN,
        "missing — `apx deploy --target apps` needs it",
        "Re-run `apx scaffold <name> --target apps`, or `apx deploy` "
        "for model-serving (no bundle required).",
    )
