# Fresh-install Hardening + `apx doctor` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the end-user adopter journey (`scaffold` → `run` → `deploy` → top-level entry) clear, actionable error messages, and add an `apx doctor` command that diagnoses the whole environment at once (with a default live workspace check).

**Architecture:** A new self-contained facts layer `apx_agent/_doctor.py` defines a `Check` dataclass and one function per diagnostic, grouped into Environment / Authentication / Project. `cli.py` consumes these for both the new `apx doctor` command and inline preflights, so one source of truth feeds both. `_doctor` references the three existing cli helpers (`_detect_target`, `_databrickscfg_profiles`) via lazy import to keep test patch targets valid and avoid circular imports.

**Tech Stack:** Python 3.11+, `click` (CLI + `click.testing.CliRunner`), `databricks.sdk` (already a dependency), stdlib `difflib`/`shutil`/`subprocess`, `pytest` + `unittest.mock`.

---

## File Structure

- **Create `python/src/apx_agent/_doctor.py`** — `Status` enum, `Check` dataclass, one `check_*` function per diagnostic, and `run_checks(cwd, *, online)` aggregator returning ordered `(group, [Check])`. Pure facts + fix hints; no printing.
- **Create `python/tests/test_doctor.py`** — unit tests per check + aggregator.
- **Modify `python/src/apx_agent/cli.py`** — add `_fix_msg` helper, `_ApxGroup` (did-you-mean), `apx doctor` command + presentation, refactor `_preflight_databricks_auth` onto the auth check, harden `run`/`deploy`/`scaffold`.
- **Modify `python/tests/test_cli.py`** — entry-level did-you-mean test, `run` pre-import probe test, scaffold footer test.
- **Modify `README.md` and `docs/getting-started.md`** — mention `apx doctor`.

> All commands below run from `python/` unless noted. Use `uv run pytest ...` / `uv run apx ...`.

---

## Task 1: `_doctor.py` core types + aggregator skeleton

**Files:**
- Create: `python/src/apx_agent/_doctor.py`
- Test: `python/tests/test_doctor.py`

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_doctor.py`:

```python
"""Tests for _doctor.py — the apx environment diagnostic layer."""

from __future__ import annotations

from pathlib import Path

from apx_agent._doctor import Check, Status, run_checks


def test_check_is_frozen_dataclass():
    c = Check(name="X", status=Status.OK, detail="fine", fix=None)
    assert c.name == "X"
    assert c.status is Status.OK
    assert c.fix is None


def test_run_checks_returns_ordered_groups(tmp_path: Path):
    groups = run_checks(tmp_path, online=False)
    names = [g for g, _ in groups]
    assert names == ["Environment", "Authentication", "Project"]
    for _group, checks in groups:
        assert all(isinstance(c, Check) for c in checks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apx_agent._doctor'`

- [ ] **Step 3: Write minimal implementation**

Create `python/src/apx_agent/_doctor.py`:

```python
"""Environment diagnostics for the apx CLI.

The *facts* layer behind `apx doctor` and the inline preflights in cli.py.
Each `check_*` function inspects one thing and returns a `Check`. cli.py owns
presentation; this module owns what's wrong and how to fix it. References to
cli helpers (`_detect_target`, `_databrickscfg_profiles`) are lazy imports so
this module has no import-time dependency on cli (cli imports this module).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path


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
```

Add stub check functions so imports resolve (they get real bodies in later tasks):

```python
def check_python_version() -> Check:
    return Check("Python", Status.SKIP, "not implemented", None)


def check_apx_install() -> Check:
    return Check("apx-agent install", Status.SKIP, "not implemented", None)


def check_uv() -> Check:
    return Check("uv", Status.SKIP, "not implemented", None)


def check_databricks_cli() -> Check:
    return Check("Databricks CLI", Status.SKIP, "not implemented", None)


def check_uvicorn() -> Check:
    return Check("uvicorn", Status.SKIP, "not implemented", None)


def check_databricks_auth() -> Check:
    return Check("Databricks auth", Status.SKIP, "not implemented", None)


def check_databricks_workspace(*, auth_ok: bool) -> Check:
    return Check("Workspace reachable", Status.SKIP, "not implemented", None)


def check_project_layout(cwd: Path) -> Check:
    return Check("Project layout", Status.SKIP, "not implemented", None)


def check_target(cwd: Path) -> Check:
    return Check("Target", Status.SKIP, "not implemented", None)


def check_extras(cwd: Path) -> Check:
    return Check("Required extra", Status.SKIP, "not implemented", None)


def check_databricks_yml(cwd: Path) -> Check:
    return Check("databricks.yml", Status.SKIP, "not implemented", None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/apx_agent/_doctor.py tests/test_doctor.py
git commit -m "feat(doctor): add Check types and run_checks aggregator skeleton"
```

---

## Task 2: Environment checks

**Files:**
- Modify: `python/src/apx_agent/_doctor.py`
- Test: `python/tests/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

Append to `python/tests/test_doctor.py`:

```python
import sys

import apx_agent._doctor as doctor


def test_python_version_ok(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 12, 2, "final", 0))
    c = doctor.check_python_version()
    assert c.status is doctor.Status.OK
    assert "3.12.2" in c.detail


def test_python_version_too_old(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 10, 9, "final", 0))
    c = doctor.check_python_version()
    assert c.status is doctor.Status.FAIL
    assert "3.11" in c.fix


def test_uv_present(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/uv")
    c = doctor.check_uv()
    assert c.status is doctor.Status.OK


def test_uv_missing_is_warn(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    c = doctor.check_uv()
    assert c.status is doctor.Status.WARN
    assert c.fix is not None


def test_databricks_cli_missing_is_warn(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    c = doctor.check_databricks_cli()
    assert c.status is doctor.Status.WARN
    assert "deploy" in c.detail


def test_uvicorn_present():
    # uvicorn is a dev/runtime dep installed in the test env.
    c = doctor.check_uvicorn()
    assert c.status in (doctor.Status.OK, doctor.Status.WARN)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_doctor.py -v -k "version or uv or cli or uvicorn"`
Expected: FAIL — checks return SKIP / `doctor.sys` may not exist yet.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/_doctor.py`, add imports at top (below `from pathlib import Path`):

```python
import importlib
import importlib.metadata
import shutil
import subprocess
import sys

MIN_PYTHON = (3, 11)
```

Replace the four stubs (`check_python_version`, `check_apx_install`, `check_uv`, `check_databricks_cli`, `check_uvicorn`) with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor.py -v -k "version or uv or cli or uvicorn"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/apx_agent/_doctor.py tests/test_doctor.py
git commit -m "feat(doctor): implement environment checks"
```

---

## Task 3: Authentication checks (offline + online)

**Files:**
- Modify: `python/src/apx_agent/_doctor.py`
- Test: `python/tests/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

Append to `python/tests/test_doctor.py`:

```python
from unittest.mock import MagicMock, patch


def test_auth_ok(monkeypatch):
    with patch("databricks.sdk.core.Config", return_value=object()):
        c = doctor.check_databricks_auth()
    assert c.status is doctor.Status.OK


def test_auth_no_profiles_first_timer(monkeypatch):
    def boom(*a, **k):
        raise ValueError("no creds")

    with patch("databricks.sdk.core.Config", side_effect=boom), patch(
        "apx_agent.cli._databrickscfg_profiles", return_value=[]
    ):
        c = doctor.check_databricks_auth()
    assert c.status is doctor.Status.FAIL
    assert "auth login" in c.fix


def test_auth_ambiguous_profiles(monkeypatch):
    def boom(*a, **k):
        raise ValueError("ambiguous")

    with patch("databricks.sdk.core.Config", side_effect=boom), patch(
        "apx_agent.cli._databrickscfg_profiles", return_value=["DEFAULT", "prod"]
    ):
        c = doctor.check_databricks_auth()
    assert c.status is doctor.Status.FAIL
    assert "DATABRICKS_CONFIG_PROFILE" in c.fix
    assert "prod" in c.fix


def test_workspace_skipped_when_auth_failed():
    c = doctor.check_databricks_workspace(auth_ok=False)
    assert c.status is doctor.Status.SKIP


def test_workspace_ok():
    me = MagicMock()
    me.user_name = "alice@example.com"
    client = MagicMock()
    client.current_user.me.return_value = me
    client.config.host = "https://x.cloud.databricks.com"
    with patch("databricks.sdk.WorkspaceClient", return_value=client):
        c = doctor.check_databricks_workspace(auth_ok=True)
    assert c.status is doctor.Status.OK
    assert "alice@example.com" in c.detail


def test_workspace_expired_token():
    client = MagicMock()
    client.current_user.me.side_effect = Exception("401 invalid access token")
    with patch("databricks.sdk.WorkspaceClient", return_value=client):
        c = doctor.check_databricks_workspace(auth_ok=True)
    assert c.status is doctor.Status.FAIL
    assert "auth login" in c.fix


def test_workspace_unreachable():
    client = MagicMock()
    client.current_user.me.side_effect = Exception("Name or service not known")
    with patch("databricks.sdk.WorkspaceClient", return_value=client):
        c = doctor.check_databricks_workspace(auth_ok=True)
    assert c.status is doctor.Status.FAIL
    assert "host" in c.fix.lower()


def test_workspace_forbidden():
    client = MagicMock()
    client.current_user.me.side_effect = Exception("403 PERMISSION_DENIED")
    with patch("databricks.sdk.WorkspaceClient", return_value=client):
        c = doctor.check_databricks_workspace(auth_ok=True)
    assert c.status is doctor.Status.FAIL
    assert "permission" in c.detail.lower() or "403" in c.detail
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_doctor.py -v -k "auth or workspace"`
Expected: FAIL — checks return SKIP.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/_doctor.py`, replace `check_databricks_auth` and `check_databricks_workspace` stubs with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor.py -v -k "auth or workspace"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/apx_agent/_doctor.py tests/test_doctor.py
git commit -m "feat(doctor): implement offline auth + live workspace checks"
```

---

## Task 4: Project checks

**Files:**
- Modify: `python/src/apx_agent/_doctor.py`
- Test: `python/tests/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

Append to `python/tests/test_doctor.py`:

```python
def _make_apps_project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.apx.agent]\nname='x'\n"
    )
    (tmp_path / "agent_server").mkdir()
    (tmp_path / "agent_server" / "start_server.py").write_text("# app\n")
    (tmp_path / "databricks.yml").write_text("bundle:\n  name: x\n")
    return tmp_path


def test_project_layout_missing(tmp_path: Path):
    c = doctor.check_project_layout(tmp_path)
    assert c.status is doctor.Status.SKIP
    assert "scaffold" in (c.fix or "")


def test_project_layout_apps(tmp_path: Path):
    _make_apps_project(tmp_path)
    c = doctor.check_project_layout(tmp_path)
    assert c.status is doctor.Status.OK


def test_target_apps(tmp_path: Path):
    _make_apps_project(tmp_path)
    c = doctor.check_target(tmp_path)
    assert c.status is doctor.Status.OK
    assert "apps" in c.detail


def test_databricks_yml_present(tmp_path: Path):
    _make_apps_project(tmp_path)
    c = doctor.check_databricks_yml(tmp_path)
    assert c.status is doctor.Status.OK


def test_databricks_yml_missing_in_project(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.apx.agent]\nname='x'\n")
    (tmp_path / "agent.py").write_text("# agent\n")
    c = doctor.check_databricks_yml(tmp_path)
    assert c.status is doctor.Status.WARN
    assert c.fix is not None


def test_databricks_yml_skip_outside_project(tmp_path: Path):
    c = doctor.check_databricks_yml(tmp_path)
    assert c.status is doctor.Status.SKIP
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_doctor.py -v -k "project_layout or target or databricks_yml"`
Expected: FAIL — checks return SKIP unconditionally.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/_doctor.py`, add a private helper and replace the four project stubs:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: PASS (all doctor tests)

- [ ] **Step 5: Commit**

```bash
git add src/apx_agent/_doctor.py tests/test_doctor.py
git commit -m "feat(doctor): implement project-layout/target/extras/databricks.yml checks"
```

---

## Task 5: `apx doctor` command + presentation

**Files:**
- Modify: `python/src/apx_agent/cli.py` (add command near the `version` command, ~line 395)
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `python/tests/test_cli.py`:

```python
def test_doctor_runs_offline_and_reports(tmp_path: Path):
    runner = CliRunner()
    with patch("apx_agent._doctor.check_databricks_auth") as auth:
        from apx_agent._doctor import Check, Status

        auth.return_value = Check("Databricks auth", Status.OK, "ok", None)
        result = runner.invoke(main, ["doctor", "--offline"])
    assert "Environment" in result.output
    assert "Authentication" in result.output
    assert "Project" in result.output


def test_doctor_exit_nonzero_on_fail():
    runner = CliRunner()
    from apx_agent._doctor import Check, Status

    fail = Check("Python", Status.FAIL, "too old", "upgrade")
    with patch("apx_agent._doctor.check_python_version", return_value=fail):
        result = runner.invoke(main, ["doctor", "--offline"])
    assert result.exit_code != 0
    assert "upgrade" in result.output


def test_doctor_json_flag():
    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--offline", "--json"])
    assert result.exit_code in (0, 1)
    payload = json.loads(result.output)
    assert "Environment" in payload
    assert isinstance(payload["Environment"], list)


def test_doctor_online_invokes_live_check():
    runner = CliRunner()
    from apx_agent._doctor import Check, Status

    with patch(
        "apx_agent._doctor.check_databricks_workspace"
    ) as ws, patch(
        "apx_agent._doctor.check_databricks_auth",
        return_value=Check("Databricks auth", Status.OK, "ok", None),
    ):
        ws.return_value = Check("Workspace reachable", Status.OK, "ok", None)
        runner.invoke(main, ["doctor"])
    assert ws.called
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v -k doctor`
Expected: FAIL — `No such command 'doctor'`.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/cli.py`, add this import near the other local imports at the top of the file (after `from typing import Any`):

```python
from . import _doctor as _doctor_mod
```

Add the command after the `version` command (after line ~395):

```python
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
    """Diagnose the apx environment: tools, auth, and project layout.

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
        raise SystemExit(1 if fails else 0)

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
            "Fix the ✗ items, then re-run `apx doctor`."
        )
        raise SystemExit(1)
    click.echo(f"All clear ({warns} warning(s)).")
```

> Note: the test patches `apx_agent._doctor.check_*`; because `run_checks` calls
> those names as module-level attributes within `_doctor`, the patches take
> effect. The `from . import _doctor as _doctor_mod` alias is only used in cli
> for `Status`/`run_checks`, so patching the underlying functions still works.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v -k doctor`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/apx_agent/cli.py tests/test_cli.py
git commit -m "feat(doctor): add `apx doctor` command with --offline/--json"
```

---

## Task 6: `_fix_msg` helper + refactor `_preflight_databricks_auth`

**Files:**
- Modify: `python/src/apx_agent/cli.py` (`_fix_msg` near top helpers ~line 110; `_preflight_databricks_auth` ~line 175)
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_cli.py`:

```python
def test_fix_msg_format():
    from apx_agent.cli import _fix_msg

    msg = _fix_msg("Title", "what happened", "do this")
    assert "Title" in msg
    assert "what happened" in msg
    assert "Fix:" in msg
    assert "do this" in msg
    assert "apx doctor" in msg


def test_preflight_auth_uses_check(monkeypatch):
    from apx_agent._doctor import Check, Status

    fail = Check("Databricks auth", Status.FAIL, "no profiles", "login here")
    with patch("apx_agent._doctor.check_databricks_auth", return_value=fail):
        with pytest.raises(click.ClickException) as exc:
            from apx_agent.cli import _preflight_databricks_auth

            _preflight_databricks_auth()
    assert "login here" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v -k "fix_msg or preflight_auth_uses_check"`
Expected: FAIL — `_fix_msg` undefined; old preflight doesn't call the check.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/cli.py`, add `_fix_msg` near the other top-level helpers (after the `import` block, ~line 50):

```python
def _fix_msg(title: str, detail: str, fix: str | None) -> str:
    """Consistent error body for hardened CLI failures."""
    parts = [title, detail]
    if fix:
        parts.append(f"\nFix:\n    {fix}")
    parts.append("\nRun `apx doctor` for a full check.")
    return "\n".join(parts)
```

Replace the body of `_preflight_databricks_auth` (keep the docstring) with a delegation to the check:

```python
def _preflight_databricks_auth() -> None:
    """Fail `apx run`/`deploy` with dev-time guidance when auth is unresolved.

    Delegates to the doctor auth check so inline errors and `apx doctor` share
    one source of truth.
    """
    from . import _doctor as _d

    result = _d.check_databricks_auth()
    if result.status is _d.Status.FAIL:
        raise click.ClickException(
            _fix_msg(
                "Could not resolve Databricks authentication. This agent "
                "connects to a workspace at startup.",
                result.detail,
                result.fix,
            )
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v -k "fix_msg or preflight"`
Expected: PASS

> Also run the existing auth test to confirm no regression:
> Run: `uv run pytest tests/test_cli.py -v -k "auth"`
> Expected: PASS (existing test at the `_databrickscfg_profiles` patch still passes;
> if it asserts exact old wording, update its assertion to match `result.fix`).

- [ ] **Step 5: Commit**

```bash
git add src/apx_agent/cli.py tests/test_cli.py
git commit -m "refactor(cli): add _fix_msg, route preflight auth through doctor check"
```

---

## Task 7: Entry-level "did you mean" group

**Files:**
- Modify: `python/src/apx_agent/cli.py` (`@click.group()` at ~line 370)
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `python/tests/test_cli.py`:

```python
def test_unknown_command_suggests_closest():
    runner = CliRunner()
    result = runner.invoke(main, ["deploy"])  # typo of deploy
    assert result.exit_code != 0
    assert "deploy" in result.output
    assert "did you mean" in result.output.lower()


def test_unknown_command_no_close_match():
    runner = CliRunner()
    result = runner.invoke(main, ["zzzzzz"])
    assert result.exit_code != 0
    # Should not crash; click's standard 'No such command' still applies.
    assert "No such command" in result.output or "zzzzzz" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v -k "unknown_command"`
Expected: FAIL — no "did you mean" text.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/cli.py`, add `import difflib` to the import block. Define the group class just above `@click.group()` (~line 369):

```python
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
            raise click.UsageError(f"No such command '{cmd_name}'.{hint}")
```

Change the group decorator from `@click.group()` to:

```python
@click.group(cls=_ApxGroup)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v -k "unknown_command"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/apx_agent/cli.py tests/test_cli.py
git commit -m "feat(cli): suggest closest command on unknown-command typo"
```

---

## Task 8: `run` pre-import probe

**Files:**
- Modify: `python/src/apx_agent/cli.py` (`run` command ~line 1193; add helper `_probe_import` nearby)
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_cli.py`:

```python
def test_run_probe_reports_broken_agent(tmp_path: Path, monkeypatch):
    # A scaffolded-looking apps project whose agent module raises on import.
    (tmp_path / "pyproject.toml").write_text("[tool.apx.agent]\nname='x'\n")
    agent_server = tmp_path / "agent_server"
    agent_server.mkdir()
    (agent_server / "__init__.py").write_text("")
    (agent_server / "start_server.py").write_text(
        "import does_not_exist_xyz\napp = None\n"
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    with patch("apx_agent.cli._preflight_databricks_auth"), patch(
        "apx_agent.cli.autolog_if_env", create=True
    ):
        result = runner.invoke(main, ["run"])
    assert result.exit_code != 0
    out = result.output
    assert "does_not_exist_xyz" in out or "start_server" in out
    assert "apx doctor" in out
```

- [ ] **Step 2: Run tests to verify it fails**

Run: `uv run pytest tests/test_cli.py -v -k "run_probe"`
Expected: FAIL — uvicorn is reached / raw error instead of friendly message.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/cli.py`, add a helper above the `run` command:

```python
def _probe_import(module_spec: str) -> None:
    """Import the ASGI module in-process to surface agent.py errors clearly.

    `module_spec` is "module:variable"; we import the module half so a broken
    `agent.py` produces a clean file+line message instead of a uvicorn
    subprocess traceback. The CWD must already be on sys.path (the caller
    ensures this for the real run via app_dir).
    """
    import importlib
    import traceback

    mod_name = module_spec.split(":", 1)[0]
    cwd = str(Path.cwd())
    added = cwd not in sys.path
    if added:
        sys.path.insert(0, cwd)
    try:
        importlib.import_module(mod_name)
    except Exception as e:  # noqa: BLE001 — surface any import-time failure
        tb = traceback.format_exc(limit=3).strip().splitlines()
        tail = tb[-1] if tb else str(e)
        raise click.ClickException(
            _fix_msg(
                f"Failed to import your agent module `{mod_name}`.",
                f"{type(e).__name__}: {e}\n    {tail}",
                "Fix the error in your agent code shown above, then re-run "
                "`apx run`.",
            )
        ) from e
    finally:
        if added:
            sys.path.remove(cwd)
```

In the `run` command body, add the probe immediately before the final `uvicorn.run(...)` call (after the autolog block):

```python
    _probe_import(module)
    uvicorn.run(module, host=host, port=port, reload=reload, app_dir=str(Path.cwd()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v -k "run_probe"`
Expected: PASS

> Regression: Run `uv run pytest tests/test_cli.py -v -k "run"` and confirm any
> existing `run` test that mocks uvicorn still passes. If an existing test
> invokes `run` against a module that can't import, add
> `patch("apx_agent.cli._probe_import")` to that test.

- [ ] **Step 5: Commit**

```bash
git add src/apx_agent/cli.py tests/test_cli.py
git commit -m "feat(run): probe agent import and report errors with file context"
```

---

## Task 9: `deploy` preflight + subprocess failure wrapping

**Files:**
- Modify: `python/src/apx_agent/cli.py` (`deploy` command ~line 1497; `_deploy_apps_impl` subprocess calls ~line 2504+)
- Test: `python/tests/test_deploy_apps.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_deploy_apps.py` (match its existing imports/fixtures; it already uses `CliRunner` + `main`):

```python
def test_deploy_blocks_when_cli_missing(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from apx_agent._doctor import Check, Status
    from apx_agent.cli import main

    # apps-looking project
    (tmp_path / "databricks.yml").write_text("bundle:\n  name: x\n")
    (tmp_path / "pyproject.toml").write_text("[tool.apx.agent]\nname='x'\n")
    (tmp_path / "agent_server").mkdir()
    monkeypatch.chdir(tmp_path)

    warn = Check("Databricks CLI", Status.WARN, "not found", "install it")
    with patch("apx_agent._doctor.check_databricks_cli", return_value=warn), patch(
        "apx_agent.cli._preflight_databricks_auth"
    ):
        result = CliRunner().invoke(main, ["deploy", "--target", "apps"])
    assert result.exit_code != 0
    assert "Databricks CLI" in result.output
    assert "install it" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deploy_apps.py -v -k "cli_missing"`
Expected: FAIL — deploy proceeds past the missing-CLI point.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/cli.py`, add a helper near `_preflight_apps`:

```python
def _preflight_databricks_cli() -> None:
    """Block deploy early if the Databricks CLI isn't installed."""
    from . import _doctor as _d

    result = _d.check_databricks_cli()
    if result.status is not _d.Status.OK:
        raise click.ClickException(
            _fix_msg(
                "`apx deploy` needs the Databricks CLI.",
                result.detail,
                result.fix,
            )
        )
```

In the `deploy` command body, call it at the very start of the deploy flow, before the bundle/apps work begins (right after `target` is resolved):

```python
    _preflight_databricks_cli()
```

Then wrap the bundle/apps subprocess failures. Find the `_run_databricks_cmd(...)` calls inside `_deploy_apps_impl` (the `bundle deploy` / `bundle run` invocations ~line 2601-2624). Where their non-zero exit is currently raised, ensure the raise routes through `_fix_msg`. Locate the existing error raise after the subprocess call and replace its message with:

```python
        raise click.ClickException(
            _fix_msg(
                "Databricks bundle deploy failed.",
                (stderr_tail or "see output above").strip(),
                "Run `apx doctor --online` to verify auth and workspace "
                "access, then retry.",
            )
        )
```

> If `_run_databricks_cmd` already raises its own `ClickException`, instead add
> a `try/except click.ClickException` around the `bundle deploy` call in
> `_deploy_apps_impl` that re-raises with the `_fix_msg`-wrapped text above
> (preserve `from e`). Keep `stderr_tail` as the last ~15 lines of captured
> stderr if available, else "".

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_deploy_apps.py -v -k "cli_missing"`
Expected: PASS

> Regression: Run `uv run pytest tests/test_deploy_apps.py -v` and confirm the
> existing deploy tests still pass (they mock the subprocess layer; the new
> preflight calls a check that, unmocked, returns OK when `databricks` is on
> PATH — if CI lacks the CLI, those tests must patch
> `apx_agent._doctor.check_databricks_cli` to return OK).

- [ ] **Step 5: Commit**

```bash
git add src/apx_agent/cli.py tests/test_deploy_apps.py
git commit -m "feat(deploy): preflight databricks CLI and wrap bundle failures"
```

---

## Task 10: `scaffold` next-steps footer

**Files:**
- Modify: `python/src/apx_agent/cli.py` (`scaffold` command ~line 1091)
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_cli.py`:

```python
def test_scaffold_prints_next_steps(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["scaffold", "my-agent"])
    assert result.exit_code == 0
    out = result.output
    assert "cd my-agent" in out
    assert "uv sync" in out
    assert "apx run" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v -k "next_steps"`
Expected: FAIL — no next-steps footer printed.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/cli.py`, at the end of the `scaffold` command body (after the file-writing loop completes successfully), add:

```python
    click.echo("")
    click.echo("Next steps:")
    click.echo(f"    cd {name}")
    click.echo("    uv sync          # install agent deps")
    click.echo("    apx run --reload # local dev server + /_apx/* dev UI")
    click.echo("")
    click.echo("Tip: run `apx doctor` to check your environment.")
```

> `name` is the scaffold command's existing argument. If the command computed a
> target subdirectory under a different variable, use that variable instead so
> the `cd` path is correct.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v -k "next_steps"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/apx_agent/cli.py tests/test_cli.py
git commit -m "feat(scaffold): print next-steps footer after scaffolding"
```

---

## Task 11: Docs — mention `apx doctor`

**Files:**
- Modify: `README.md` (Quick start, after the TypeScript block ~"See docs/getting-started.md")
- Modify: `docs/getting-started.md`

- [ ] **Step 1: Add the troubleshooting note to README**

In `README.md`, immediately after the line `See [docs/getting-started.md](docs/getting-started.md) for the longer walkthrough.`, add:

```markdown
> **Something not working?** Run `apx doctor` — it checks your Python, uv,
> Databricks CLI, authentication (including a live workspace round-trip), and
> project layout, and prints exactly what to fix. Add `--offline` to skip the
> network check.
```

- [ ] **Step 2: Add a Troubleshooting section to getting-started**

In `docs/getting-started.md`, add a `## Troubleshooting` section near the end:

```markdown
## Troubleshooting

Run `apx doctor` at any point to diagnose your environment:

```bash
apx doctor            # full check incl. a live workspace round-trip
apx doctor --offline  # skip the network check (CI / offline)
apx doctor --json     # machine-readable output
```

It reports Python version, `uv`, the Databricks CLI, authentication, and your
project layout, with a `Fix:` line for anything wrong. Exit code is non-zero if
any check fails, so it is safe to use in CI preflights.
```

- [ ] **Step 3: Verify docs render / links intact**

Run: `grep -n "apx doctor" README.md docs/getting-started.md`
Expected: matches in both files.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/getting-started.md
git commit -m "docs(setup): document apx doctor in quick start + getting-started"
```

---

## Final verification

- [ ] **Run the full doctor + cli + deploy test suites**

Run: `uv run pytest tests/test_doctor.py tests/test_cli.py tests/test_deploy_apps.py -v`
Expected: all PASS.

- [ ] **Smoke-test the real command**

Run: `uv run apx doctor --offline`
Expected: grouped checklist prints; exit code reflects your environment.

Run: `uv run apx deploy --help` and `uv run apx run --help`
Expected: help prints without import errors (confirms `_ApxGroup` + new imports load).

- [ ] **Lint (if configured)**

Run: `uv run ruff check src/apx_agent/_doctor.py src/apx_agent/cli.py` (skip if ruff absent)
Expected: clean.

---

## Self-Review notes (for the implementer)

- **Spec coverage:** Every spec check (`python_version`, `apx_install`, `uv`,
  `databricks_cli`, `databricks_auth`, `databricks_workspace`, `project_layout`,
  `target`, `extras`, `uvicorn`, `databricks_yml`) maps to Tasks 2–4. `doctor`
  command + `--offline`/`--json` → Task 5. `_fix_msg` + auth refactor → Task 6.
  Entry did-you-mean → Task 7. `run` probe → Task 8. `deploy` preflight + wrap
  → Task 9. `scaffold` footer → Task 10. Docs → Task 11.
- **Type consistency:** `Check(name, status, detail, fix)` and
  `Status.{OK,WARN,FAIL,SKIP}` are used identically across all tasks;
  `run_checks(cwd, *, online)` signature matches its one call site in Task 5.
- **Known integration risk (flagged):** Tasks 6, 8, 9 modify existing command
  bodies whose exact surrounding code wasn't fully quoted here. The implementer
  must read the current `run`/`deploy` bodies and the `_run_databricks_cmd`
  error path before editing, and adjust insertion points / variable names
  (`name`, `stderr_tail`) to match. Each such step calls this out inline.
