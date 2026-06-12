"""Tests for ``apx-agent deploy --target apps`` — the Databricks Asset Bundle path.

The Apps deploy flow shells out to the ``databricks`` CLI. Every subprocess
call routes through ``apx_agent.cli._run_databricks_cmd``, which gives a
single seam to mock. We never spawn a real subprocess from these tests.

Each test builds a minimal scaffold under ``tmp_path``:

    databricks.yml          — bundle doc with a single app entry
    pyproject.toml          — present so pre-flight passes
    agent.py                — top-level (ADK-style) defines stub `agent`
    agent_server/__init__.py — framework boilerplate dir (empty here)

Then ``CliRunner.invoke(main, ["agents", "deploy", "--target", "apps", ...])`` runs
against that cwd. Subprocess outputs are stubbed by patching
``apx_agent.cli._run_databricks_cmd``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from unittest.mock import patch

from apx_agent.cli import main


# ---------------------------------------------------------------------------
# Scaffold fixture
# ---------------------------------------------------------------------------


_DATABRICKS_YML = """\
bundle:
  name: test-app

resources:
  apps:
    my-app:
      name: my-app
      description: test
      source_code_path: ./.build

targets:
  dev:
    default: true
    mode: development
"""


_AGENT_PY = """\
class _StubAgent:
    \"\"\"Minimal agent stub for tests — never invoked.\"\"\"

    def __init__(self) -> None:
        self._tool_fns = []
        self._sub_agent_urls = []


agent = _StubAgent()
"""


def _write_scaffold(tmp_path: Path, *, with_yml: bool = True) -> Path:
    """Write a minimal Apps-shaped project under ``tmp_path``.

    Mirrors the ADK-style scaffold: stub agent at top-level ``agent.py``,
    framework boilerplate dir ``agent_server/`` exists for the pre-flight
    check but is otherwise empty.

    Returns ``tmp_path`` for convenience.
    """
    if with_yml:
        (tmp_path / "databricks.yml").write_text(_DATABRICKS_YML)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test-app"\n')
    (tmp_path / "agent.py").write_text(_AGENT_PY)
    server = tmp_path / "agent_server"
    server.mkdir()
    (server / "__init__.py").write_text("")
    return tmp_path


@pytest.fixture
def scaffold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a scaffold under tmp_path, chdir into it, expose it on sys.path."""
    _write_scaffold(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    # Drop any cached agent / agent_server modules from a previous test
    sys.modules.pop("agent", None)
    sys.modules.pop("agent_server", None)
    sys.modules.pop("agent_server.agent", None)
    return tmp_path


# ---------------------------------------------------------------------------
# Subprocess mock plumbing
# ---------------------------------------------------------------------------


class _FakeProc:
    """Stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ready_payload(url: str = "https://my-app.example.databricksapps.com") -> str:
    return json.dumps({
        "name": "my-app",
        "url": url,
        "compute_status": {"state": "ACTIVE"},
        "app_status": {"state": "RUNNING"},
    })


def _install_subprocess_mock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    validate_rc: int = 0,
    deploy_rc: int = 0,
    run_rc: int = 0,
    get_payload: str | None = None,
    get_states: list[tuple[str, str]] | None = None,
) -> list[list[str]]:
    """Patch ``_run_databricks_cmd`` and return the captured args log."""
    calls: list[list[str]] = []
    payload = get_payload if get_payload is not None else _ready_payload()

    # Build a per-state iterator for apps get if a sequence was requested.
    state_iter: Any = None
    if get_states is not None:
        def _build() -> Any:
            for compute, app in get_states:
                yield json.dumps({
                    "url": "https://my-app.example.databricksapps.com",
                    "compute_status": {"state": compute},
                    "app_status": {"state": app},
                })
        state_iter = iter(_build())

    def fake(args: list[str], profile: str | None = None) -> _FakeProc:
        calls.append(list(args))
        if args[:2] == ["bundle", "validate"]:
            return _FakeProc(validate_rc, stdout="ok\n", stderr="validate err\n")
        if args[:2] == ["bundle", "deploy"]:
            return _FakeProc(deploy_rc, stdout="deploy ok\n", stderr="deploy err\n")
        if args[:2] == ["bundle", "run"]:
            return _FakeProc(run_rc, stdout="run ok\n", stderr="run err\n")
        if args[:2] == ["apps", "get"]:
            if state_iter is not None:
                try:
                    return _FakeProc(0, stdout=next(state_iter), stderr="")
                except StopIteration:
                    return _FakeProc(0, stdout=payload, stderr="")
            return _FakeProc(0, stdout=payload, stderr="")
        return _FakeProc(0, stdout="", stderr="")

    monkeypatch.setattr("apx_agent.cli._run_databricks_cmd", fake)
    # The readyz deploy gate (default ON) issues an authenticated GET against
    # the live app's /readyz after it reaches RUNNING. It has dedicated unit
    # coverage in test_cli.py; here we stub it to "ready" so these pipeline
    # tests stay focused on the bundle validate → deploy → run → poll flow and
    # don't try to mint a token / hit the network.
    monkeypatch.setattr(
        "apx_agent.cli._check_readyz",
        lambda app_url, *, profile, **_kw: (True, {}),
    )
    # The Databricks-CLI presence preflight (`shutil.which("databricks")`) is
    # exercised separately in test_deploy_blocks_when_cli_missing; here we
    # simulate the CLI being installed so these tests are deterministic in CI,
    # which has no `databricks` binary on PATH.
    monkeypatch.setattr("apx_agent.cli._preflight_databricks_cli", lambda: None)
    # Make sleeps a no-op so the polling tests run fast.
    monkeypatch.setattr("apx_agent.cli.time.sleep", lambda *_a, **_k: None) \
        if False else None  # noqa: SIM114 — sleep is imported inside _poll_app_ready

    import apx_agent.cli as cli_mod
    if hasattr(cli_mod, "time"):
        monkeypatch.setattr(cli_mod.time, "sleep", lambda *_a, **_k: None,
                            raising=False)
    return calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable real sleeping inside the polling loop in every test."""
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _stub_compile_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend ``apx_agent._responses_agent`` exists so validation passes.

    Individual tests that want to exercise the "missing extra" failure path
    override this by un-doing the stub.
    """
    import types

    stub = types.ModuleType("apx_agent._responses_agent")
    stub.compile_to_responses_agent = lambda *_a, **_k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "apx_agent._responses_agent", stub)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_target_apps_triggers_bundle_deploy(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: --target apps runs validate → deploy → run → apps get."""
    calls = _install_subprocess_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, [
        "agents", "deploy", "--target", "apps", "--bundle-target", "dev",
    ])
    assert result.exit_code == 0, result.output
    seq = [c[:2] for c in calls]
    assert ["bundle", "validate"] in seq
    assert ["bundle", "deploy"] in seq
    assert ["bundle", "run"] in seq
    assert ["apps", "get"] in seq
    # default (non-JSON) mode prints the URL on stdout
    assert "databricksapps.com" in result.output


def test_preflight_fails_without_databricks_yml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No databricks.yml in cwd → friendly error + exit 1."""
    # Only write pyproject + agent_server; omit databricks.yml.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test-app"\n')
    server = tmp_path / "agent_server"
    server.mkdir()
    (server / "__init__.py").write_text("")
    (server / "agent.py").write_text(_AGENT_PY)
    monkeypatch.chdir(tmp_path)

    _install_subprocess_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps"])
    assert result.exit_code != 0
    assert "databricks.yml" in result.output


def test_validate_failure_surfaces_friendly_error(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero `bundle validate` exit raises ClickException + tail."""
    _install_subprocess_mock(monkeypatch, validate_rc=2)
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps"])
    assert result.exit_code != 0
    assert "bundle validate" in result.output


def test_no_run_skips_bundle_run(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-run elides the `databricks bundle run` subprocess."""
    calls = _install_subprocess_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps", "--no-run"])
    assert result.exit_code == 0, result.output
    seq = [c[:2] for c in calls]
    assert ["bundle", "run"] not in seq
    # validate, deploy, and apps get still happened
    assert ["bundle", "validate"] in seq
    assert ["bundle", "deploy"] in seq
    assert ["apps", "get"] in seq


def test_auto_update_yml_adds_missing_resources(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--auto-update-yml merges ResourceSpec entries into databricks.yml."""
    # Override the stub agent (top-level agent.py per the ADK-style layout)
    # with one that declares a ResourceSpec.
    (scaffold / "agent.py").write_text(textwrap.dedent("""\
        from apx_agent._resources import ResourceSpec

        class _StubAgent:
            def __init__(self):
                self._tool_fns = [_make_tool()]
                self._sub_agent_urls = []

        def _make_tool():
            def t(): return "ok"
            t._apx_resources = [
                ResourceSpec("serving_endpoint", "claude-3-5"),
                ResourceSpec("sql_warehouse", "abc123"),
            ]
            return t

        agent = _StubAgent()
        """))
    sys.modules.pop("agent", None)
    # Make the agent look like an LlmAgent so the resource walker finds tools.
    monkeypatch.setattr(
        "apx_agent._resources._iter_tool_fns",
        lambda agent: iter(agent._tool_fns),
    )
    monkeypatch.setattr(
        "apx_agent._resources._iter_sub_agents",
        lambda agent: iter([]),
    )
    _install_subprocess_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, [
        "agents", "deploy", "--target", "apps", "--auto-update-yml",
    ])
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load((scaffold / "databricks.yml").read_text())
    resources = doc["resources"]["apps"]["my-app"]["resources"]
    # Each entry is {"<resource_type>": {"name": ..., ...}}.
    rendered = [next(iter(r.values())) for r in resources]
    names = [r["name"] for r in rendered]
    assert any("claude-3-5" in n for n in names)
    assert any("abc123" in n for n in names)
    # Identifiers should match what we asked for.
    endpoint_block = next(r for r in resources if "serving_endpoint" in r)
    assert endpoint_block["serving_endpoint"]["endpoint_name"] == "claude-3-5"
    warehouse_block = next(r for r in resources if "sql_warehouse" in r)
    assert warehouse_block["sql_warehouse"]["id"] == "abc123"


def test_auto_update_yml_preserves_user_added_resources(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-added resources with matching names are NOT clobbered."""
    # Pre-populate the bundle with an explicit resource that uses the
    # same auto-derived name we'd generate. The DAB shape is
    # {"<resource_type>": {"name": "...", ...}} — name is NESTED.
    doc = yaml.safe_load((scaffold / "databricks.yml").read_text())
    doc["resources"]["apps"]["my-app"]["resources"] = [
        {
            "serving_endpoint": {
                "name": "claude-3-5-endpoint",  # matches the auto-derived name
                "endpoint_name": "USER-OVERRIDE-claude",
                "permission": "CAN_QUERY",
            },
        },
    ]
    (scaffold / "databricks.yml").write_text(
        yaml.safe_dump(doc, default_flow_style=False, sort_keys=False),
    )

    (scaffold / "agent.py").write_text(textwrap.dedent("""\
        from apx_agent._resources import ResourceSpec

        class _StubAgent:
            def __init__(self):
                self._tool_fns = [_make_tool()]
                self._sub_agent_urls = []

        def _make_tool():
            def t(): return "ok"
            t._apx_resources = [
                ResourceSpec("serving_endpoint", "claude-3-5"),
            ]
            return t

        agent = _StubAgent()
        """))
    sys.modules.pop("agent", None)
    monkeypatch.setattr(
        "apx_agent._resources._iter_tool_fns",
        lambda agent: iter(agent._tool_fns),
    )
    monkeypatch.setattr(
        "apx_agent._resources._iter_sub_agents",
        lambda agent: iter([]),
    )
    _install_subprocess_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, [
        "agents", "deploy", "--target", "apps", "--auto-update-yml",
    ])
    assert result.exit_code == 0, result.output

    updated = yaml.safe_load((scaffold / "databricks.yml").read_text())
    resources = updated["resources"]["apps"]["my-app"]["resources"]
    # Exactly one entry with that name — the user's, untouched.
    def _name(e: dict) -> str:
        for v in e.values():
            if isinstance(v, dict) and isinstance(v.get("name"), str):
                return v["name"]
        return ""
    matching = [r for r in resources if _name(r) == "claude-3-5-endpoint"]
    assert len(matching) == 1
    # The user's override is preserved verbatim.
    assert matching[0]["serving_endpoint"]["endpoint_name"] == "USER-OVERRIDE-claude"


def test_polling_stops_on_active_running(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling exits as soon as compute=ACTIVE AND app=RUNNING."""
    calls = _install_subprocess_mock(
        monkeypatch,
        get_states=[
            ("STARTING", "DEPLOYING"),
            ("STARTING", "DEPLOYING"),
            ("ACTIVE", "RUNNING"),
        ],
    )
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps", "--no-run"])
    assert result.exit_code == 0, result.output
    # Three apps get calls, then stop.
    get_calls = [c for c in calls if c[:2] == ["apps", "get"]]
    assert len(get_calls) == 3


def test_polling_times_out(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the app never reaches ACTIVE/RUNNING, polling raises after 300s."""
    # Always-pending state — never resolves to ACTIVE/RUNNING.
    _install_subprocess_mock(
        monkeypatch,
        get_payload=json.dumps({
            "url": "https://my-app.example.databricksapps.com",
            "compute_status": {"state": "STARTING"},
            "app_status": {"state": "DEPLOYING"},
        }),
    )
    # Compress the timeout by patching the deadline math: replace time.time
    # so that the second call is already past the deadline.
    import itertools
    counter = itertools.count(start=0, step=1000)
    monkeypatch.setattr("apx_agent.cli.time.time", lambda: next(counter))

    runner = CliRunner()
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps", "--no-run"])
    assert result.exit_code != 0
    assert "Timed out" in result.output


def test_json_output_shape(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json-output emits a single JSON blob on stdout with expected keys."""
    _install_subprocess_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, [
        "agents", "deploy", "--target", "apps", "--bundle-target", "prod",
        "--json-output",
    ])
    assert result.exit_code == 0, result.output
    # The stdout will only contain the JSON line — progress went to stderr.
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["app_name"] == "my-app"
    assert payload["bundle_target"] == "prod"
    assert "deploy_seconds" in payload
    assert "run_seconds" in payload
    assert "app_url" in payload


def test_readyz_gate_fails_deploy_when_degraded(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded /readyz fails the deploy loudly (gate ON by default)."""
    _install_subprocess_mock(monkeypatch)
    # Override the default "ready" stub with a degraded result.
    monkeypatch.setattr(
        "apx_agent.cli._check_readyz",
        lambda app_url, *, profile, **_kw: (False, {"llm": "fail"}),
    )
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps"])
    assert result.exit_code != 0
    assert "readyz gate failed" in result.output
    assert "--no-readyz-gate" in result.output


def test_no_readyz_gate_skips_check(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-readyz-gate skips the /readyz call entirely."""
    _install_subprocess_mock(monkeypatch)
    called = {"n": 0}

    def _boom(app_url, *, profile, **_kw):
        called["n"] += 1
        return (False, {"llm": "fail"})

    monkeypatch.setattr("apx_agent.cli._check_readyz", _boom)
    runner = CliRunner()
    result = runner.invoke(
        main, ["agents", "deploy", "--target", "apps", "--no-readyz-gate"]
    )
    assert result.exit_code == 0, result.output
    assert called["n"] == 0


def test_missing_responses_agent_module_surfaces_friendly_error(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If mlflow.genai isn't installed, --target apps fails with a clear msg.

    Simulates the ImportError by intercepting the import of
    ``mlflow.genai.agent_server``, which is the actual runtime dep that
    ``_validate_responses_agent_compiler`` probes.
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        if name == "mlflow.genai.agent_server" or (
            name == "mlflow.genai" and "agent_server" in (fromlist or ())
        ):
            raise ImportError("No module named 'mlflow.genai.agent_server'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "mlflow.genai.agent_server", raising=False)

    _install_subprocess_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps"])
    assert result.exit_code != 0
    assert "apx-agent[eval]" in result.output


def test_terminal_error_state_fails_fast(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compute=ERROR or app=CRASHED short-circuits the poll loop."""
    _install_subprocess_mock(
        monkeypatch,
        get_states=[
            ("STARTING", "DEPLOYING"),
            ("ERROR", "UNAVAILABLE"),
        ],
    )
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps", "--no-run"])
    assert result.exit_code != 0
    assert "terminal failure" in result.output.lower() or "ERROR" in result.output


def test_app_name_resolved_from_databricks_yml(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When bundle key == app name, both subprocess calls use that string."""
    # Rewrite the yml with a different app name (and matching inner name).
    doc = yaml.safe_load((scaffold / "databricks.yml").read_text())
    apps_block = doc["resources"]["apps"]
    apps_block["renamed-app"] = apps_block.pop("my-app")
    apps_block["renamed-app"]["name"] = "renamed-app"
    (scaffold / "databricks.yml").write_text(
        yaml.safe_dump(doc, default_flow_style=False, sort_keys=False),
    )

    calls = _install_subprocess_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps"])
    assert result.exit_code == 0, result.output
    run_calls = [c for c in calls if c[:2] == ["bundle", "run"]]
    assert run_calls, "expected at least one bundle run call"
    assert "renamed-app" in run_calls[0]
    get_calls = [c for c in calls if c[:2] == ["apps", "get"]]
    assert "renamed-app" in get_calls[0]


def test_bundle_key_and_app_name_can_differ(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the DAB key ≠ inner ``name:``, route each to the right subprocess.

    ``bundle run`` consumes the bundle key. ``apps get`` consumes the
    workspace app name. Mishandling this breaks deploys for the standard
    DAB convention (e.g. ``foo-app:`` keyed, ``name: foo``).
    """
    doc = yaml.safe_load((scaffold / "databricks.yml").read_text())
    apps_block = doc["resources"]["apps"]
    block = apps_block.pop("my-app")
    block["name"] = "entity-resolution-agent"
    apps_block["entity-resolution-agent-app"] = block
    (scaffold / "databricks.yml").write_text(
        yaml.safe_dump(doc, default_flow_style=False, sort_keys=False),
    )

    calls = _install_subprocess_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps"])
    assert result.exit_code == 0, result.output

    run_calls = [c for c in calls if c[:2] == ["bundle", "run"]]
    assert run_calls
    # bundle run consumes the bundle KEY
    assert "entity-resolution-agent-app" in run_calls[0]
    assert "entity-resolution-agent" not in run_calls[0] or \
        run_calls[0].index("entity-resolution-agent-app") < \
        (run_calls[0].index("entity-resolution-agent")
         if "entity-resolution-agent" in run_calls[0] else 999)

    get_calls = [c for c in calls if c[:2] == ["apps", "get"]]
    assert get_calls
    # apps get consumes the workspace app NAME — NOT the bundle key
    assert "entity-resolution-agent" in get_calls[0]
    assert "entity-resolution-agent-app" not in get_calls[0]


def test_json_output_on_error_path(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json-output emits {ok: false, error: ...} on failure too."""
    _install_subprocess_mock(monkeypatch, validate_rc=2)
    runner = CliRunner()
    result = runner.invoke(main, [
        "agents", "deploy", "--target", "apps", "--json-output",
    ])
    assert result.exit_code != 0
    # The last line of stdout should be the JSON envelope.
    last = result.output.strip().splitlines()[-1]
    payload = json.loads(last)
    assert payload["ok"] is False
    assert "error" in payload


def test_profile_is_passed_through(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--profile X reaches _run_databricks_cmd on every subprocess call."""
    seen_profiles: list[str | None] = []

    def fake(args: list[str], profile: str | None = None) -> _FakeProc:
        seen_profiles.append(profile)
        if args[:2] == ["apps", "get"]:
            return _FakeProc(0, stdout=_ready_payload(), stderr="")
        return _FakeProc(0, stdout="ok\n", stderr="")

    monkeypatch.setattr("apx_agent.cli._run_databricks_cmd", fake)
    # Simulate the Databricks CLI being installed (CI has no `databricks`
    # binary); the presence preflight is covered by test_deploy_blocks_when_cli_missing.
    monkeypatch.setattr("apx_agent.cli._preflight_databricks_cli", lambda: None)
    # Stub the readyz gate (default ON) so this test stays focused on profile
    # threading through the bundle/apps subprocess calls.
    monkeypatch.setattr(
        "apx_agent.cli._check_readyz",
        lambda app_url, *, profile, **_kw: (True, {}),
    )
    runner = CliRunner()
    result = runner.invoke(main, [
        "agents", "deploy", "--target", "apps", "--profile", "demo-profile",
    ])
    assert result.exit_code == 0, result.output
    assert all(p == "demo-profile" for p in seen_profiles), seen_profiles


# ---------------------------------------------------------------------------
# Databricks CLI preflight (Task 9)
# ---------------------------------------------------------------------------


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
        result = CliRunner().invoke(main, ["agents", "deploy", "--target", "apps"])
    assert result.exit_code != 0
    assert "Databricks CLI" in result.output
    assert "install it" in result.output
