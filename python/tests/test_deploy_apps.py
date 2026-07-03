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


# ---------------------------------------------------------------------------
# UC version-manifest registration (apps → UC registry shim, P1)
# ---------------------------------------------------------------------------


_PYPROJECT_WITH_AGENT = (
    '[project]\nname = "test-app"\n\n'
    '[tool.apx.agent]\n'
    'model = "databricks-claude-sonnet-4-6"\n'
    'name = "My App"\n'
)


def test_register_uc_skips_with_notice_when_unconfigured(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare apps deploy (no UC name / model in config) still succeeds, and the
    skip is announced with an actionable notice — not silent."""
    called: list[Any] = []
    monkeypatch.setattr(
        "apx_agent._apps_registry.register_apps_manifest",
        lambda *a, **k: called.append((a, k)),
    )
    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, ["agents", "deploy", "--target", "apps"])
    assert result.exit_code == 0, result.output
    assert not called, "registrar must not run when no UC name resolves"
    assert "UC registration skipped" in result.output
    assert "--uc-name" in result.output  # the notice names the fix


def test_register_uc_runs_once_when_configured(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a model in config + an explicit --uc-name, the registrar runs once,
    with the resolved name + model, and serving is never promoted."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)
    calls: list[dict[str, Any]] = []

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        calls.append({
            "uc_name": uc_name, "model": model,
            "app_name": app_name, "bundle_target": bundle_target,
        })
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="3", app_name=app_name)

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _fake_registrar)
    # Avoid importing/finalizing the stub agent — out of scope for this test.
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda module: object())

    calls_log = _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps",
        "--uc-name", "main.agents.my_app",
    ])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1, f"expected exactly one registration, got {calls}"
    assert calls[0]["uc_name"] == "main.agents.my_app"
    assert calls[0]["model"] == "databricks-claude-sonnet-4-6"
    assert calls[0]["app_name"] == "my-app"
    assert "registered main.agents.my_app version 3" in result.output
    # The apps path must never promote to a serving endpoint.
    assert not any(c[:2] == ["serving-endpoints", "create"] for c in calls_log)


def test_register_uc_failure_is_non_fatal(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registrar exception logs a warning but the deploy still exits 0 —
    the App is already live; a missing ledger entry must not redden a deploy."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("UC write denied")

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _boom)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda module: object())

    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps",
        "--uc-name", "main.agents.my_app",
    ])
    assert result.exit_code == 0, result.output
    assert "UC registration failed" in result.output
    assert "non-fatal" in result.output


def test_no_register_uc_skips_the_step(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-register-uc elides registration entirely, even when configured."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)
    called: list[Any] = []
    monkeypatch.setattr(
        "apx_agent._apps_registry.register_apps_manifest",
        lambda *a, **k: called.append((a, k)),
    )
    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps",
        "--uc-name", "main.agents.my_app", "--no-register-uc",
    ])
    assert result.exit_code == 0, result.output
    assert not called
    assert "skipping the UC version-manifest registration" in result.output


def test_app_name_override_polls_override_name(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When app_name_override is set, `apps get` targets the override name."""
    from apx_agent import cli as cli_mod

    calls = _install_subprocess_mock(monkeypatch)
    logs: list[str] = []
    cli_mod._deploy_apps_impl(
        cwd=scaffold, module="agent:agent", profile=None,
        bundle_target="canary-v42", no_run=False, auto_update_yml=False,
        auto_build_wheel=False, auto_experiment=False, vars=(),
        json_output=False, readyz_gate=False, register_uc=False,
        uc_name=None, app_name_override="my-app-canary-v42",
        log=logs.append,
    )
    get_calls = [c for c in calls if c[:2] == ["apps", "get"]]
    assert get_calls, "expected an apps get call"
    assert all("my-app-canary-v42" in c for c in get_calls)


def test_register_uc_forwards_extra_version_tags(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """extra_version_tags passed to _deploy_apps_impl reach register_apps_manifest."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)
    seen: dict[str, Any] = {}

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        seen["tags"] = extra_version_tags
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="1", app_name=app_name)

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _fake_registrar)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    _install_subprocess_mock(monkeypatch)

    from apx_agent import cli as cli_mod
    cli_mod._deploy_apps_impl(
        cwd=scaffold, module="agent:agent", profile=None, bundle_target="canary-v42",
        no_run=False, auto_update_yml=False, auto_build_wheel=False,
        auto_experiment=False, vars=(), json_output=False, readyz_gate=False,
        register_uc=True, uc_name="main.agents.my_app",
        app_name_override="my-app-canary-v42",
        extra_version_tags={"apx.apps.role": "canary"},
        log=lambda *_a: None,
    )
    assert seen["tags"] == {"apx.apps.role": "canary"}


# ---------------------------------------------------------------------------
# readyz failure: ledger + recovery path (issue #401)
# ---------------------------------------------------------------------------


def _degraded_readyz(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the default 'ready' stub with a degraded /readyz result."""
    monkeypatch.setattr(
        "apx_agent.cli._check_readyz",
        lambda app_url, *, profile, **_kw: (False, {"llm": "fail"}),
    )


def test_readyz_failure_registers_manifest_with_failed_tag(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed readyz gate still ledgers the deploy: the UC manifest is
    registered, tagged apx.apps.readyz=failed, and the deploy exits non-zero
    naming the live URL (issue #401)."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)
    seen: dict[str, Any] = {}

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        seen["tags"] = extra_version_tags
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="7", app_name=app_name)

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _fake_registrar)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    # No prior @prod version — keep the recovery hint on the generic path.
    monkeypatch.setattr(
        "apx_agent._apps_registry.get_prod_alias_version", lambda uc, **k: None,
    )
    _install_subprocess_mock(monkeypatch)
    _degraded_readyz(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps", "--uc-name", "main.agents.my_app",
    ])
    assert result.exit_code != 0
    # The bad deploy is ledgered, marked as failed.
    assert seen["tags"] == {"apx.apps.readyz": "failed"}
    # The error says the app is live, where, and how to recover.
    assert "readyz gate failed" in result.output
    assert "STILL LIVE" in result.output
    assert "https://my-app.example.databricksapps.com" in result.output
    assert "re-deploy a known-good commit" in result.output


def test_readyz_failure_names_rollback_when_prior_prod_exists(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a prior @prod version with recorded git provenance exists, the
    readyz failure names the exact canary rollback command."""
    monkeypatch.setattr(
        "apx_agent._apps_registry.get_prod_alias_version", lambda uc, **k: "5",
    )
    monkeypatch.setattr(
        "apx_agent._apps_registry.get_version_git_sha",
        lambda uc, v, **k: "aaa111",
    )
    _install_subprocess_mock(monkeypatch)
    _degraded_readyz(monkeypatch)

    # Default scaffold pyproject has no model, so UC registration skips with a
    # notice — but the --uc-name override still resolves for the hint lookup.
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps",
        "--uc-name", "main.agents.my_app", "--profile", "demo-profile",
    ])
    assert result.exit_code != 0
    assert "readyz gate failed" in result.output
    assert (
        "apx-agent canary rollback --target apps --to-version 5 "
        "--profile demo-profile"
    ) in result.output
    assert "main.agents.my_app" in result.output


def test_readyz_failure_recovery_hint_lookup_never_masks_the_failure(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throwing prior-version lookup must not replace the readyz error."""
    def _boom(uc, **k):
        raise RuntimeError("UC unreachable")

    monkeypatch.setattr("apx_agent._apps_registry.get_prod_alias_version", _boom)
    _install_subprocess_mock(monkeypatch)
    _degraded_readyz(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps", "--uc-name", "main.agents.my_app",
    ])
    assert result.exit_code != 0
    assert "readyz gate failed" in result.output
    assert "recovery-hint lookup failed (non-fatal): UC unreachable" in result.output
    # Falls back to the generic recovery path.
    assert "re-deploy a known-good commit" in result.output


def test_readyz_failure_json_output_carries_url_and_detail(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json-output on a readyz failure emits app_url + readyz checks."""
    _install_subprocess_mock(monkeypatch)
    _degraded_readyz(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps", "--json-output",
    ])
    assert result.exit_code != 0
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["app_name"] == "my-app"
    assert payload["app_url"] == "https://my-app.example.databricksapps.com"
    assert payload["readyz"] == {"llm": "fail"}
    assert "readyz gate failed" in payload["error"]


def test_canary_deploy_apps_uses_full_path(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`apx canary deploy --target apps` runs validate + deploy + poll against
    the canary target and the canary app name — i.e. the faithful path."""
    calls = _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "canary", "deploy", "--target", "apps", "--canary-version", "v42",
    ])
    assert result.exit_code == 0, result.output
    seq = [c for c in calls]
    # Deployed under the canary target.
    assert any(c[:2] == ["bundle", "deploy"] and "canary-v42" in c for c in seq), seq
    # Validate ran (the thin canary path used to skip it).
    assert any(c[:2] == ["bundle", "validate"] for c in seq), seq
    # Polled the CANARY app name, not prod "my-app".
    get_calls = [c for c in seq if c[:2] == ["apps", "get"]]
    assert get_calls and all("my-app-canary-v42" in c for c in get_calls), get_calls
    # canary target written into the bundle.
    assert "canary-v42" in (scaffold / "databricks.yml").read_text()


_PYPROJECT_WITH_UC = (
    '[project]\nname = "test-app"\n\n'
    '[tool.apx.agent]\n'
    'model = "databricks-claude-sonnet-4-6"\n'
    'name = "My App"\n'
    'registered_model = "main.agents.my_app"\n'
)


def test_canary_deploy_apps_registers_role_tag_end_to_end(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: `apx canary deploy --target apps` carries apx.apps.role=canary
    all the way to the registrar, against the canary App name. Proves the tag
    survives the whole CLI path (CLI -> deploy_canary_app -> deploy_fn ->
    _deploy_apps_impl -> _register_apps_manifest_step -> register_apps_manifest),
    not just hop-by-hop."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_UC)
    seen: dict[str, Any] = {}

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        seen.update(tags=extra_version_tags, app_name=app_name,
                    uc_name=uc_name, bundle_target=bundle_target)
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="1", app_name=app_name)

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _fake_registrar)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    _install_subprocess_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "canary", "deploy", "--target", "apps", "--canary-version", "v42",
    ])
    assert result.exit_code == 0, result.output
    # The role tag reached the registrar via the full CLI path...
    assert seen.get("tags") == {"apx.apps.role": "canary"}
    # ...registered against the CANARY app name + the resolved UC name + target.
    assert seen.get("app_name") == "my-app-canary-v42"
    assert seen.get("uc_name") == "main.agents.my_app"
    assert seen.get("bundle_target") == "canary-v42"


def test_canary_deploy_apps_stamps_git_sha_end_to_end(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 end-to-end: the captured git SHA reaches the registrar as
    apx.apps.git_sha through the whole canary-deploy CLI path."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_UC)
    seen: dict[str, Any] = {}

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        seen["tags"] = extra_version_tags
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="1", app_name=app_name)

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _fake_registrar)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    monkeypatch.setattr("apx_agent.cli._git_head_sha", lambda cwd: "deadbeefcafe1234")
    _install_subprocess_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "canary", "deploy", "--target", "apps", "--canary-version", "v42",
    ])
    assert result.exit_code == 0, result.output
    assert seen.get("tags") == {
        "apx.apps.role": "canary",
        "apx.apps.git_sha": "deadbeefcafe1234",
    }


def test_canary_deploy_apps_no_git_sha_still_succeeds(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: no git SHA available (not a repo) -> deploy still succeeds, only the
    role tag is sent (no git_sha tag), and a notice is logged."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_UC)
    seen: dict[str, Any] = {}

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        seen["tags"] = extra_version_tags
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="1", app_name=app_name)

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _fake_registrar)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    monkeypatch.setattr("apx_agent.cli._git_head_sha", lambda cwd: None)
    _install_subprocess_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "canary", "deploy", "--target", "apps", "--canary-version", "v42",
    ])
    assert result.exit_code == 0, result.output
    assert seen.get("tags") == {"apx.apps.role": "canary"}
    assert "no git SHA captured" in result.output


# ---------------------------------------------------------------------------
# P2: gate-don't-mutate promote (apps)
# ---------------------------------------------------------------------------


def _setup_promote_mocks(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch, *,
    canary_sha: str | None, head_sha: str | None, dirty: bool = False,
    canary_exists: bool = True,
) -> dict[str, Any]:
    """Wire the common promote mocks. Returns a dict capturing alias/teardown."""
    from apx_agent._apps_registry import CanaryManifest

    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_UC)
    captured: dict[str, Any] = {"alias": None, "teardown": 0, "registered": None}

    cm = CanaryManifest(version="4", git_sha=canary_sha) if canary_exists else None
    monkeypatch.setattr("apx_agent.find_latest_canary_version", lambda uc, **k: cm)
    monkeypatch.setattr("apx_agent.get_prod_alias_version", lambda uc, **k: "2")
    monkeypatch.setattr("apx_agent.get_latest_prod_version", lambda uc, **k: "5")

    def _set_alias(uc, v, **k):
        captured["alias"] = (uc, v)
    monkeypatch.setattr("apx_agent.set_prod_alias_version", _set_alias)

    def _teardown(**k):
        captured["teardown"] += 1
        return True
    monkeypatch.setattr("apx_agent._canary_apps._teardown_canary", _teardown)

    monkeypatch.setattr("apx_agent.cli._git_head_sha", lambda cwd: head_sha)
    monkeypatch.setattr("apx_agent.cli._git_is_dirty", lambda cwd: dirty)

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        captured["registered"] = extra_version_tags
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="5", app_name=app_name)
    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _fake_registrar)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    _install_subprocess_mock(monkeypatch)
    return captured


def test_promote_apps_refuses_when_head_mismatches_canary_sha(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _setup_promote_mocks(scaffold, monkeypatch, canary_sha="aaa111", head_sha="bbb222")
    result = CliRunner().invoke(main, [
        "canary", "promote", "--target", "apps", "--canary-version", "v42",
    ])
    assert result.exit_code != 0
    assert "git checkout aaa111" in result.output
    assert cap["alias"] is None  # never moved prod
    assert cap["teardown"] == 0  # never tore down canary


def test_promote_apps_refuses_when_tree_dirty(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _setup_promote_mocks(scaffold, monkeypatch, canary_sha="aaa111",
                               head_sha="aaa111", dirty=True)
    result = CliRunner().invoke(main, [
        "canary", "promote", "--target", "apps", "--canary-version", "v42",
    ])
    assert result.exit_code != 0
    assert "uncommitted changes" in result.output
    assert cap["alias"] is None


def test_promote_apps_errors_when_no_canary(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _setup_promote_mocks(scaffold, monkeypatch, canary_sha=None,
                               head_sha="aaa111", canary_exists=False)
    result = CliRunner().invoke(main, [
        "canary", "promote", "--target", "apps", "--canary-version", "v42",
    ])
    assert result.exit_code != 0
    assert "No canary manifest" in result.output


def test_promote_apps_errors_when_canary_has_no_sha(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _setup_promote_mocks(scaffold, monkeypatch, canary_sha=None, head_sha="aaa111")
    result = CliRunner().invoke(main, [
        "canary", "promote", "--target", "apps", "--canary-version", "v42",
    ])
    assert result.exit_code != 0
    assert "no recorded git SHA" in result.output


def test_promote_apps_happy_path_sets_alias_and_teardown(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _setup_promote_mocks(scaffold, monkeypatch, canary_sha="aaa111", head_sha="aaa111")
    result = CliRunner().invoke(main, [
        "canary", "promote", "--target", "apps", "--canary-version", "v42",
    ])
    assert result.exit_code == 0, result.output
    # Prod re-deployed via the shared path, tagged role=prod + the soaked SHA.
    assert cap["registered"] == {"apx.apps.role": "prod", "apx.apps.git_sha": "aaa111"}
    # @prod alias moved to the new prod version; canary torn down.
    assert cap["alias"] == ("main.agents.my_app", "5")
    assert cap["teardown"] == 1
    assert "Promoted canary (commit aaa111" in result.output


def test_promote_apps_keep_canary_skips_teardown(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _setup_promote_mocks(scaffold, monkeypatch, canary_sha="aaa111", head_sha="aaa111")
    result = CliRunner().invoke(main, [
        "canary", "promote", "--target", "apps", "--canary-version", "v42", "--keep-canary",
    ])
    assert result.exit_code == 0, result.output
    assert cap["teardown"] == 0
    assert cap["alias"] == ("main.agents.my_app", "5")


# ---------------------------------------------------------------------------
# P2: gate-don't-mutate rollback + apps status
# ---------------------------------------------------------------------------


def _setup_rollback_mocks(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch, *,
    version_sha: str | None, head_sha: str | None,
) -> dict[str, Any]:
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_UC)
    captured: dict[str, Any] = {"alias": None, "registered": None}

    monkeypatch.setattr("apx_agent.get_version_git_sha", lambda uc, v, **k: version_sha)
    monkeypatch.setattr("apx_agent.get_prod_alias_version", lambda uc, **k: "5")
    monkeypatch.setattr("apx_agent.get_latest_prod_version", lambda uc, **k: "8")

    def _set_alias(uc, v, **k):
        captured["alias"] = (uc, v)
    monkeypatch.setattr("apx_agent.set_prod_alias_version", _set_alias)

    monkeypatch.setattr("apx_agent.cli._git_head_sha", lambda cwd: head_sha)
    monkeypatch.setattr("apx_agent.cli._git_is_dirty", lambda cwd: False)

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        captured["registered"] = extra_version_tags
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="8", app_name=app_name)
    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _fake_registrar)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    _install_subprocess_mock(monkeypatch)
    return captured


def test_rollback_apps_requires_to_version(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_rollback_mocks(scaffold, monkeypatch, version_sha="aaa", head_sha="aaa")
    result = CliRunner().invoke(main, ["canary", "rollback", "--target", "apps"])
    assert result.exit_code != 0
    assert "--to-version is required" in result.output


def test_rollback_apps_refuses_when_head_mismatches(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _setup_rollback_mocks(scaffold, monkeypatch, version_sha="aaa111", head_sha="bbb222")
    result = CliRunner().invoke(main, [
        "canary", "rollback", "--target", "apps", "--to-version", "3",
    ])
    assert result.exit_code != 0
    assert "git checkout aaa111" in result.output
    assert cap["alias"] is None


def test_rollback_apps_errors_when_version_has_no_sha(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_rollback_mocks(scaffold, monkeypatch, version_sha=None, head_sha="aaa")
    result = CliRunner().invoke(main, [
        "canary", "rollback", "--target", "apps", "--to-version", "3",
    ])
    assert result.exit_code != 0
    assert "no recorded apx.apps.git_sha" in result.output


def test_rollback_apps_happy_path_redeploys_and_sets_alias(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _setup_rollback_mocks(scaffold, monkeypatch, version_sha="aaa111", head_sha="aaa111")
    result = CliRunner().invoke(main, [
        "canary", "rollback", "--target", "apps", "--to-version", "3",
    ])
    assert result.exit_code == 0, result.output
    assert cap["registered"] == {"apx.apps.role": "prod", "apx.apps.git_sha": "aaa111"}
    assert cap["alias"] == ("main.agents.my_app", "8")
    assert "Rolled back prod App my-app to the commit of version 3" in result.output


def test_status_apps_shows_prod_and_canary(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apx_agent._apps_registry import CanaryManifest

    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_UC)
    monkeypatch.setattr("apx_agent.get_prod_alias_version", lambda uc, **k: "5")
    monkeypatch.setattr("apx_agent.get_version_git_sha", lambda uc, v, **k: "prodsha12345")
    monkeypatch.setattr(
        "apx_agent.find_latest_canary_version",
        lambda uc, **k: CanaryManifest(version="8", git_sha="canarysha6789"),
    )
    result = CliRunner().invoke(main, ["canary", "status", "--target", "apps"])
    assert result.exit_code == 0, result.output
    assert "@prod  → version 5" in result.output
    assert "canary → version 8" in result.output
    assert "promote ships canarysha678" in result.output
