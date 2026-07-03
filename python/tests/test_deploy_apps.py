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
    """--no-run elides the `databricks bundle run` subprocess AND the
    readiness poll — the app was never started, so `apps get` would just
    burn the 300s timeout (issue #413)."""
    calls = _install_subprocess_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps", "--no-run"])
    assert result.exit_code == 0, result.output
    seq = [c[:2] for c in calls]
    assert ["bundle", "run"] not in seq
    assert ["apps", "get"] not in seq
    # validate and deploy still happened
    assert ["bundle", "validate"] in seq
    assert ["bundle", "deploy"] in seq
    # ...and the deploy says how to start the app.
    assert "app not started (--no-run)" in result.output
    assert "databricks bundle run" in result.output


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
    # No --no-run: it now skips the poll entirely (issue #413); bundle run is
    # mocked to rc 0 so the poll is still what this test exercises.
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps"])
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
    # No --no-run: it now skips the poll entirely (issue #413).
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps"])
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


def test_json_output_enriched_with_uc_version_provenance_readyz(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success JSON records what shipped (issue #417): uc_name, registered
    version, git/lock provenance threaded from the manifest tags, and the
    readyz checks — and stdout is EXACTLY one JSON object."""
    import hashlib

    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)
    (scaffold / "uv.lock").write_text("version = 1\n")

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="3", app_name=app_name)

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _fake_registrar)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    monkeypatch.setattr("apx_agent.cli._git_head_sha", lambda cwd: "a" * 40)
    monkeypatch.setattr("apx_agent.cli._git_is_dirty", lambda cwd: False)
    _install_subprocess_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps",
        "--uc-name", "main.agents.my_app", "--json-output",
    ])
    assert result.exit_code == 0, result.output
    # stdout carries exactly one JSON object — progress went to stderr.
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["uc_name"] == "main.agents.my_app"
    assert payload["version"] == "3"
    assert payload["git_sha"] == "a" * 40
    assert payload["git_dirty"] is False
    assert payload["lock_sha256"] == hashlib.sha256(
        (scaffold / "uv.lock").read_bytes()
    ).hexdigest()
    assert payload["readyz"] == {}  # the stubbed gate returned (True, {})


def test_json_output_no_run_has_null_readyz(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-run never probes /readyz, so the JSON reports readyz: null (not a
    fake pass), and registration was skipped so version is null too."""
    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps", "--no-run", "--json-output",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["readyz"] is None
    assert payload["version"] is None  # no UC name configured → not registered


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
    # No --no-run: it now skips the poll entirely (issue #413).
    result = runner.invoke(main, ["agents", "deploy", "--target", "apps"])
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


def test_register_uc_failure_notice_names_register_repair(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When deploy-time registration fails, the non-fatal notice names the
    exact single-agent repair command — `apx-agent agents register` with the
    deploy's --uc-name/--profile — not just 'register manually' (issue #418)."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("UC write denied")

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _boom)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda module: object())

    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps",
        "--uc-name", "main.agents.my_app", "--profile", "my-prof",
    ])
    assert result.exit_code == 0, result.output
    assert (
        "apx-agent agents register --uc-name main.agents.my_app --profile my-prof"
        in result.output
    )


# ---------------------------------------------------------------------------
# agents register — single-agent UC manifest backfill (issue #418)
# ---------------------------------------------------------------------------


def _install_workspace_mock(
    monkeypatch: pytest.MonkeyPatch, *, app_exists: bool = True,
) -> list[str]:
    """Patch ``_connect_workspace`` with a fake ws; return the apps.get log."""
    got: list[str] = []

    class _Apps:
        def get(self, name: str) -> Any:
            got.append(name)
            if not app_exists:
                raise RuntimeError(f"no app named {name}")
            return object()

    class _WS:
        apps = _Apps()

    monkeypatch.setattr(
        "apx_agent.cli._connect_workspace", lambda profile: (_WS(), None),
    )
    return got


def test_agents_register_backfills_manifest(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: verifies the app exists, registers via the same registrar
    the deploy uses (with fresh provenance tags), and echoes the version."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)
    calls: list[dict[str, Any]] = []

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        calls.append({
            "uc_name": uc_name, "model": model, "app_name": app_name,
            "bundle_target": bundle_target, "agent_name": agent_name,
            "extra_version_tags": extra_version_tags,
        })
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="5", app_name=app_name)

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _fake_registrar)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    monkeypatch.setattr(
        "apx_agent.cli._provenance_version_tags",
        lambda cwd: {"apx.apps.git_sha": "abc123"},
    )
    apps_got = _install_workspace_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "register", "--uc-name", "main.agents.my_app", "-y",
    ])
    assert result.exit_code == 0, result.output
    assert apps_got == ["my-app"], "must verify the app exists before ledgering"
    assert len(calls) == 1, f"expected exactly one registration, got {calls}"
    assert calls[0]["uc_name"] == "main.agents.my_app"
    assert calls[0]["model"] == "databricks-claude-sonnet-4-6"
    assert calls[0]["app_name"] == "my-app"
    assert calls[0]["bundle_target"] == "dev"
    assert calls[0]["extra_version_tags"] == {"apx.apps.git_sha": "abc123"}
    assert "registered main.agents.my_app version 5" in result.output


def test_agents_register_json_output(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json emits a single result object on stdout, per the mutation-command
    convention (issue #417); --bundle-target lands on the record."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="9", app_name=app_name)

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _fake_registrar)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    _install_workspace_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "register", "--uc-name", "main.agents.my_app",
        "--bundle-target", "prod", "-y", "--json",
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "uc_name": "main.agents.my_app",
        "version": "9",
        "app_name": "my-app",
        "bundle_target": "prod",
    }


def test_agents_register_refuses_missing_app(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the app doesn't exist in the workspace, register exits non-zero
    and never touches the registrar — no ledger entry for a nonexistent app."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)
    called: list[Any] = []
    monkeypatch.setattr(
        "apx_agent._apps_registry.register_apps_manifest",
        lambda *a, **k: called.append((a, k)),
    )
    _install_workspace_mock(monkeypatch, app_exists=False)

    result = CliRunner().invoke(main, [
        "agents", "register", "--uc-name", "main.agents.my_app", "-y",
    ])
    assert result.exit_code != 0
    assert not called, "registrar must not run for a nonexistent app"
    assert "refusing to register" in result.output
    assert "agents deploy --target apps" in result.output  # names the fix


def test_agents_register_failure_is_fatal(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike the deploy-time best-effort step, a registrar failure here exits
    non-zero — a backfill has no green deploy to protect. Also exercises the
    confirmation prompt (no -y)."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("UC write denied")

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _boom)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    _install_workspace_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "register", "--uc-name", "main.agents.my_app",
    ], input="y\n")
    assert result.exit_code != 0
    assert "UC registration failed" in result.output
    assert "UC write denied" in result.output


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


def test_promote_apps_json_success(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json on a successful promote: stdout is EXACTLY one JSON object with
    the alias move + soaked commit; all progress went to stderr (issue #417)."""
    _setup_promote_mocks(scaffold, monkeypatch, canary_sha="aaa111", head_sha="aaa111")
    result = CliRunner().invoke(main, [
        "canary", "promote", "--target", "apps", "--canary-version", "v42", "--json",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["target"] == "apps"
    assert payload["uc_name"] == "main.agents.my_app"
    assert payload["app_name"] == "my-app"
    assert payload["git_sha"] == "aaa111"
    assert payload["prod_version"] == "5"
    assert payload["previous_prod_version"] == "2"
    assert payload["canary_removed"] is True


def test_promote_apps_json_failure(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json on a refused promote (HEAD != soaked SHA): stdout is EXACTLY one
    {ok: false, error: ...} object and the exit code is non-zero."""
    cap = _setup_promote_mocks(scaffold, monkeypatch, canary_sha="aaa111", head_sha="bbb222")
    result = CliRunner().invoke(main, [
        "canary", "promote", "--target", "apps", "--canary-version", "v42", "--json",
    ])
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "git checkout aaa111" in payload["error"]
    assert cap["alias"] is None  # never moved prod


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


def test_status_apps_json(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """canary status --target apps --json emits exactly one JSON object with
    the prod/canary versions + commits (issue #417)."""
    from apx_agent._apps_registry import CanaryManifest

    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_UC)
    monkeypatch.setattr("apx_agent.get_prod_alias_version", lambda uc, **k: "5")
    monkeypatch.setattr("apx_agent.get_version_git_sha", lambda uc, v, **k: "prodsha12345")
    monkeypatch.setattr(
        "apx_agent.find_latest_canary_version",
        lambda uc, **k: CanaryManifest(version="8", git_sha="canarysha6789"),
    )
    result = CliRunner().invoke(main, ["canary", "status", "--target", "apps", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "target": "apps",
        "uc_name": "main.agents.my_app",
        "prod_version": "5",
        "prod_git_sha": "prodsha12345",
        "canary_version": "8",
        "canary_git_sha": "canarysha6789",
    }


# ---------------------------------------------------------------------------
# Deploy provenance stamping (issue #403)
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


def test_provenance_tags_from_a_real_git_repo(tmp_path: Path) -> None:
    """_provenance_version_tags reads the actual HEAD sha, dirty state, and
    uv.lock hash back from a real repo — claim vs reality, not a mock."""
    import hashlib

    from apx_agent.cli import _provenance_version_tags

    _git(["init"], tmp_path)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)

    tags = _provenance_version_tags(tmp_path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert tags["apx.apps.git_sha"] == head
    assert tags["apx.git_dirty"] == "false"
    assert tags["apx.lock_sha256"] == hashlib.sha256(
        (tmp_path / "uv.lock").read_bytes()
    ).hexdigest()

    # An uncommitted file flips the dirty flag on the next capture.
    (tmp_path / "extra.txt").write_text("uncommitted\n")
    assert _provenance_version_tags(tmp_path)["apx.git_dirty"] == "true"


def test_provenance_tags_omitted_outside_a_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No git repo and no uv.lock → empty tags, never an error."""
    from apx_agent.cli import _provenance_version_tags

    # Make sure git can't walk up into an enclosing repo.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert _provenance_version_tags(tmp_path) == {}


def test_plain_deploy_threads_provenance_to_registrar(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain `deploy --target apps` stamps git sha + dirty flag + lock hash
    on the registered manifest version (issue #403)."""
    import hashlib

    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)
    (scaffold / "uv.lock").write_text("version = 1\n")
    seen: dict[str, Any] = {}

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        seen["tags"] = extra_version_tags
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="9", app_name=app_name)

    monkeypatch.setattr(
        "apx_agent._apps_registry.register_apps_manifest", _fake_registrar,
    )
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    monkeypatch.setattr("apx_agent.cli._git_head_sha", lambda cwd: "a" * 40)
    monkeypatch.setattr("apx_agent.cli._git_is_dirty", lambda cwd: True)
    _install_subprocess_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps",
        "--uc-name", "main.agents.my_app",
    ])
    assert result.exit_code == 0, result.output
    assert seen["tags"] == {
        "apx.apps.git_sha": "a" * 40,
        "apx.git_dirty": "true",
        "apx.lock_sha256": hashlib.sha256(
            (scaffold / "uv.lock").read_bytes()
        ).hexdigest(),
    }

# ---------------------------------------------------------------------------
# issue #413: deploy flag hygiene — warn on ignored flags, --no-run skips poll
# ---------------------------------------------------------------------------


def test_experiment_flag_warns_under_apps_target(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--experiment is model-serving-only; under --target apps the deploy
    succeeds but warns loudly and points at the apps-flow equivalent."""
    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps",
        "--experiment", "/Users/me/my-exp",
    ])
    assert result.exit_code == 0, result.output
    assert "--experiment is ignored with --target apps" in result.output
    # The warning names the apps-flow equivalents.
    assert "mlflow_experiment_id" in result.output
    assert "--auto-experiment" in result.output


def test_model_serving_only_flags_warn_when_explicit_under_apps(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicitly passing model-serving-only flags under --target apps prints
    one combined warning naming every ignored flag."""
    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps",
        "--no-publish-tools", "--agent-name", "my-agent",
        "--scale-to-zero", "--workload-size", "Small",
        "--env-suffix", "staging",
    ])
    assert result.exit_code == 0, result.output
    assert "ignored with --target apps" in result.output
    assert "--publish-tools/--no-publish-tools" in result.output
    assert "--agent-name" in result.output
    # #407: scale + env-suffix flags are model-serving-only too.
    assert "--scale-to-zero/--no-scale-to-zero" in result.output
    assert "--workload-size" in result.output
    assert "--env-suffix" in result.output
    # Flags left at their defaults are NOT named.
    assert "--no-deploy" not in result.output


def test_default_apps_deploy_does_not_warn_about_ignored_flags(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain apps deploy (all model-serving-only flags at defaults) prints
    no ignored-flag warnings — defaults are detected via ParameterSource, not
    value comparison."""
    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, ["agents", "deploy", "--target", "apps"])
    assert result.exit_code == 0, result.output
    assert "ignored with --target apps" not in result.output
    assert "--experiment is ignored" not in result.output


def test_no_run_skips_readiness_poll_and_still_registers_manifest(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-run never calls _poll_app_ready or the readyz gate (the app was
    never started), but the UC manifest registration still runs — it records
    intent/version and doesn't need the app up."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)
    registered: list[dict[str, Any]] = []

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        registered.append({"uc_name": uc_name, "app_name": app_name})
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="1", app_name=app_name)

    monkeypatch.setattr(
        "apx_agent._apps_registry.register_apps_manifest", _fake_registrar,
    )
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    _install_subprocess_mock(monkeypatch)

    with patch("apx_agent.cli._poll_app_ready") as poll, \
         patch("apx_agent.cli._check_readyz") as readyz:
        result = CliRunner().invoke(main, [
            "agents", "deploy", "--target", "apps", "--no-run",
            "--uc-name", "main.agents.my_app",
        ])
    assert result.exit_code == 0, result.output
    poll.assert_not_called()
    readyz.assert_not_called()
    assert registered == [{"uc_name": "main.agents.my_app", "app_name": "my-app"}]
    assert "app not started (--no-run)" in result.output


def test_canary_deploy_apps_explicit_traffic_warns(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit --traffic under `canary deploy --target apps` warns that Apps
    has no platform traffic split — the value is a recorded hint only."""
    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "canary", "deploy", "--target", "apps",
        "--canary-version", "v42", "--traffic", "25",
    ])
    assert result.exit_code == 0, result.output
    assert "no platform-level traffic split" in result.output


def test_canary_deploy_apps_default_traffic_does_not_warn(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leaving --traffic at its default prints no traffic warning for apps."""
    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "canary", "deploy", "--target", "apps", "--canary-version", "v42",
    ])
    assert result.exit_code == 0, result.output
    assert "no platform-level traffic split" not in result.output


# ---------------------------------------------------------------------------
# issue #404: version correlation — inject APX_GIT_SHA into the app env
# ---------------------------------------------------------------------------


_DATABRICKS_YML_WITH_CORRELATION_VAR = _DATABRICKS_YML + """
variables:
  apx_git_sha:
    default: ""
"""


def _bundle_deploy_call(calls: list[list[str]]) -> list[str]:
    return next(c for c in calls if c[:2] == ["bundle", "deploy"])


def test_deploy_injects_apx_git_sha_var_when_bundle_declares_it(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the bundle declares the `apx_git_sha` variable (post-#404
    scaffolds), deploy passes the HEAD sha as `--var apx_git_sha=<sha>` so the
    app container gets APX_GIT_SHA and traces carry version identity."""
    (scaffold / "databricks.yml").write_text(_DATABRICKS_YML_WITH_CORRELATION_VAR)
    monkeypatch.setattr("apx_agent.cli._git_head_sha", lambda cwd: "e" * 40)
    monkeypatch.setattr("apx_agent.cli._git_is_dirty", lambda cwd: False)
    calls = _install_subprocess_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps", "--bundle-target", "dev",
    ])
    assert result.exit_code == 0, result.output
    deploy_call = _bundle_deploy_call(calls)
    var_values = [
        deploy_call[i + 1]
        for i, a in enumerate(deploy_call)
        if a == "--var"
    ]
    assert f"apx_git_sha={'e' * 40}" in var_values


def test_deploy_skips_apx_git_sha_var_for_pre_404_bundles(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bundles that don't declare the variable (pre-#404 scaffolds) deploy
    untouched — passing an undeclared --var would fail `bundle deploy`."""
    monkeypatch.setattr("apx_agent.cli._git_head_sha", lambda cwd: "e" * 40)
    monkeypatch.setattr("apx_agent.cli._git_is_dirty", lambda cwd: False)
    calls = _install_subprocess_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps", "--bundle-target", "dev",
    ])
    assert result.exit_code == 0, result.output
    deploy_call = _bundle_deploy_call(calls)
    assert not any(a.startswith("apx_git_sha=") for a in deploy_call)


def test_deploy_explicit_apx_git_sha_var_wins_over_injection(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator-passed `--var apx_git_sha=` is forwarded verbatim and the
    auto-injection stands down (no duplicate --var)."""
    (scaffold / "databricks.yml").write_text(_DATABRICKS_YML_WITH_CORRELATION_VAR)
    monkeypatch.setattr("apx_agent.cli._git_head_sha", lambda cwd: "e" * 40)
    monkeypatch.setattr("apx_agent.cli._git_is_dirty", lambda cwd: False)
    calls = _install_subprocess_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps", "--bundle-target", "dev",
        "--var", "apx_git_sha=operator-pinned",
    ])
    assert result.exit_code == 0, result.output
    deploy_call = _bundle_deploy_call(calls)
    sha_vars = [a for a in deploy_call if a.startswith("apx_git_sha=")]
    assert sha_vars == ["apx_git_sha=operator-pinned"]


def test_deploy_skips_apx_git_sha_var_outside_a_git_repo(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No HEAD sha (not a git repo) → nothing to inject, even when the bundle
    declares the variable; the yml default ("") applies."""
    (scaffold / "databricks.yml").write_text(_DATABRICKS_YML_WITH_CORRELATION_VAR)
    monkeypatch.setattr("apx_agent.cli._git_head_sha", lambda cwd: None)
    calls = _install_subprocess_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps", "--bundle-target", "dev",
    ])
    assert result.exit_code == 0, result.output
    deploy_call = _bundle_deploy_call(calls)
    assert not any(a.startswith("apx_git_sha=") for a in deploy_call)


# ---------------------------------------------------------------------------
# issue #415: --env / --secret-env runtime config for apps deploys
# ---------------------------------------------------------------------------


def test_env_flag_merges_into_bundle_config_env(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--env KEY=VALUE lands in resources.apps.<key>.config.env before the
    bundle deploy, key NAMES are echoed, and values are never echoed."""
    calls = _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps",
        "--env", "FEATURE_FLAG=on",
        "--env", "LOG_LEVEL=debug",
    ])
    assert result.exit_code == 0, result.output

    updated = yaml.safe_load((scaffold / "databricks.yml").read_text())
    env = updated["resources"]["apps"]["my-app"]["config"]["env"]
    assert {"name": "FEATURE_FLAG", "value": "on"} in env
    assert {"name": "LOG_LEVEL", "value": "debug"} in env
    # The merge happened BEFORE bundle validate/deploy ran.
    assert [c[:2] for c in calls][0] == ["bundle", "validate"]
    # Key names are echoed; values are not.
    assert "FEATURE_FLAG" in result.output
    assert "LOG_LEVEL" in result.output
    assert "debug" not in result.output
    # Persistence semantics are stated plainly.
    assert "persists" in result.output


def test_env_flag_never_clobbers_user_authored_entry(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing config.env entry with the same name is left untouched —
    the deploy warns and skips instead of clobbering."""
    doc = yaml.safe_load((scaffold / "databricks.yml").read_text())
    doc["resources"]["apps"]["my-app"]["config"] = {
        "env": [{"name": "FEATURE_FLAG", "value": "user-set"}],
    }
    (scaffold / "databricks.yml").write_text(
        yaml.safe_dump(doc, default_flow_style=False, sort_keys=False),
    )
    _install_subprocess_mock(monkeypatch)

    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps",
        "--env", "FEATURE_FLAG=clobber-attempt",
    ])
    assert result.exit_code == 0, result.output

    updated = yaml.safe_load((scaffold / "databricks.yml").read_text())
    env = updated["resources"]["apps"]["my-app"]["config"]["env"]
    matching = [e for e in env if e["name"] == "FEATURE_FLAG"]
    assert matching == [{"name": "FEATURE_FLAG", "value": "user-set"}]
    assert "clobber-attempt" not in (scaffold / "databricks.yml").read_text()
    assert "skipped" in result.output
    assert "FEATURE_FLAG" in result.output


def test_secret_env_emits_value_from_reference_never_a_value(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--secret-env KEY=scope/key writes a secret app resource (READ) plus a
    valueFrom env entry — no literal value anywhere in the yml or output."""
    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps",
        "--secret-env", "OPENAI_API_KEY=llm-secrets/openai",
    ])
    assert result.exit_code == 0, result.output

    updated = yaml.safe_load((scaffold / "databricks.yml").read_text())
    app = updated["resources"]["apps"]["my-app"]
    env_entry = [
        e for e in app["config"]["env"] if e["name"] == "OPENAI_API_KEY"
    ]
    assert env_entry == [
        {"name": "OPENAI_API_KEY", "valueFrom": "secret-openai-api-key"},
    ]
    # The env entry is a reference — it must not carry a literal value.
    assert "value" not in env_entry[0]
    secret_res = [
        r for r in app["resources"] if r.get("name") == "secret-openai-api-key"
    ]
    assert len(secret_res) == 1
    assert secret_res[0]["secret"] == {
        "scope": "llm-secrets", "key": "openai", "permission": "READ",
    }
    assert "OPENAI_API_KEY" in result.output


def test_env_flags_warn_and_are_ignored_under_model_serving(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--env / --secret-env are apps-only; --target model-serving warns and
    never touches databricks.yml (mirror of the #413 hygiene warnings)."""
    before = (scaffold / "databricks.yml").read_text()
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "model-serving",
        "--env", "FEATURE_FLAG=on",
        "--secret-env", "TOK=scope/key",
    ])
    # The warning fires before the --model requirement aborts the command.
    assert "ignored with --target model-serving" in result.output
    assert "--env" in result.output
    assert "--secret-env" in result.output
    assert (scaffold / "databricks.yml").read_text() == before


def test_model_serving_without_env_flags_does_not_warn(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaults are detected via ParameterSource — a plain model-serving
    invocation prints no apps-only-flag warning."""
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "model-serving",
    ])
    assert "ignored with --target model-serving" not in result.output


def test_json_output_lists_env_key_names_only(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The JSON summary carries the injected key NAMES — never values."""
    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps", "--json-output",
        "--env", "FEATURE_FLAG=super-sensitive",
        "--secret-env", "OPENAI_API_KEY=llm-secrets/openai",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["env_keys"] == ["FEATURE_FLAG"]
    assert payload["secret_env_keys"] == ["OPENAI_API_KEY"]
    assert "super-sensitive" not in result.output
    assert "super-sensitive" not in result.stdout


def test_json_output_without_env_flags_has_empty_key_lists(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No --env/--secret-env → the summary keys are present but empty."""
    _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps", "--json-output",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["env_keys"] == []
    assert payload["secret_env_keys"] == []


@pytest.mark.parametrize("flag,raw,expect", [
    ("--env", "NO_EQUALS", "KEY=VALUE"),
    ("--env", "1BAD=x", "KEY=VALUE"),
    ("--secret-env", "TOK=noslash", "scope/key"),
    ("--secret-env", "TOK=/key", "scope/key"),
    ("--secret-env", "TOK=scope/", "scope/key"),
])
def test_malformed_env_flags_fail_fast(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
    flag: str, raw: str, expect: str,
) -> None:
    """Malformed pairs abort the deploy with a message naming the shape."""
    calls = _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "agents", "deploy", "--target", "apps", flag, raw,
    ])
    assert result.exit_code != 0
    assert expect in result.output
    # Nothing was deployed and the yml was not rewritten with a bad entry.
    assert ["bundle", "deploy"] not in [c[:2] for c in calls]
    assert raw.split("=", 1)[0] not in (scaffold / "databricks.yml").read_text()
