"""`doctor --durable` runs the project offline and reports per-stage readiness."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from apx_agent._doctor import Check, Status

PYPROJECT = """\
[project]
name = "{name}"
version = "0.1.0"

[tool.apx.agent]
name = "{name}"
model = "databricks-claude-sonnet-4-6"
module = "agent:agent"
"""


def _project(tmp_path: Path, name: str, agent_py: str, *, extra_files: dict[str, str] | None = None) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "pyproject.toml").write_text(PYPROJECT.format(name=name))
    (root / "agent.py").write_text(agent_py)
    for rel, text in (extra_files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def _by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def _snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


LLM_AGENT = "from apx_agent import LlmAgent\nagent = LlmAgent(name='{name}', tools=[])\n"


def test_non_apx_project_is_a_single_skip(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = tmp_path / "plain"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "plain"\nversion = "0.1.0"\n')
    checks = check_durable_readiness(root)
    assert [(c.name, c.status) for c in checks] == [("Config", Status.SKIP)]


def test_agent_import_error_fails_config_and_skips_rest(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = _project(tmp_path, "broken", "raise RuntimeError('boom at import')\n")
    checks = _by_name(check_durable_readiness(root))
    assert checks["Config"].status is Status.FAIL
    assert "boom at import" in checks["Config"].detail
    for stage in ("Compile", "Startup", "Request", "Deploy"):
        assert checks[stage].status is Status.SKIP
        assert checks[stage].detail == "blocked by Config"


def test_raw_request_tool_fails_compile(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    agent_py = (
        "from apx_agent import Dependencies, LlmAgent\n"
        "def peek(question: str, request: Dependencies.Request) -> str:\n"
        "    '''Read the raw request.'''\n"
        "    return question\n"
        "agent = LlmAgent(name='raw', tools=[peek])\n"
    )
    checks = _by_name(check_durable_readiness(_project(tmp_path, "raw", agent_py)))
    assert checks["Config"].status is Status.OK
    assert checks["Compile"].status is Status.FAIL
    assert "user_identity" in checks["Compile"].detail
    assert checks["Startup"].detail == "blocked by Compile"


def test_two_projects_in_one_process_import_their_own_agent(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    a = _by_name(check_durable_readiness(_project(tmp_path, "alpha", LLM_AGENT.format(name="alpha"))))
    b = _by_name(check_durable_readiness(_project(tmp_path, "beta", LLM_AGENT.format(name="beta"))))
    assert "alpha" in a["Config"].detail
    assert "beta" in b["Config"].detail


def test_isolation_restores_state_after_failure(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = _project(tmp_path, "broken2", "raise RuntimeError('boom')\n")
    env, cwd, path, files = dict(os.environ), Path.cwd(), list(sys.path), _snapshot(root)
    check_durable_readiness(root)
    assert dict(os.environ) == env
    assert Path.cwd() == cwd
    assert sys.path == path
    assert _snapshot(root) == files
    mod = sys.modules.get("agent")
    assert mod is None or not str(mod.__file__).startswith(str(root))


def _modules_inside(root: Path) -> list[str]:
    inside = []
    for name, mod in list(sys.modules.items()):
        file = getattr(mod, "__file__", None)
        if file is not None and Path(file).resolve().is_relative_to(root.resolve()):
            inside.append(name)
    return inside


def test_successful_import_leaves_no_project_modules(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = _project(tmp_path, "clean", LLM_AGENT.format(name="clean"), extra_files={"helper.py": "X = 1\n"})
    checks = _by_name(check_durable_readiness(root))
    assert checks["Config"].status is Status.OK
    assert _modules_inside(root) == []


def test_outbound_http_is_refused(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    agent_py = "import httpx\nhttpx.get('https://example.com')\n" + LLM_AGENT.format(name="net")
    checks = _by_name(check_durable_readiness(_project(tmp_path, "net", agent_py)))
    assert checks["Config"].status is Status.FAIL
    assert "ConnectError" in checks["Config"].detail
    assert "refused" in checks["Config"].detail


@pytest.mark.parametrize(
    "build",
    [
        "from databricks.sdk import WorkspaceClient\nws = WorkspaceClient()\n",
        "import databricks.sdk\nws = databricks.sdk.WorkspaceClient()\n",
        "from apx_agent._wiring import _make_workspace_client\nws = _make_workspace_client()\n",
        "from apx_agent._defaults import _make_workspace_client\nws = _make_workspace_client(host='x')\n",
        "from apx_agent._dev import WorkspaceClient\nws = WorkspaceClient()\n",
    ],
)
def test_workspace_clients_are_fakes(tmp_path: Path, build: str) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    agent_py = (
        build
        + "from apx_agent import LlmAgent\n"
        + "assert ws.config.host == 'https://apx-doctor.invalid', ws.config.host\n"
        + "agent = LlmAgent(name='wsfake', tools=[])\n"
    )
    checks = _by_name(check_durable_readiness(_project(tmp_path, "wsfake", agent_py)))
    assert checks["Config"].status is Status.OK, checks["Config"].detail


def test_workspace_client_patches_are_restored(tmp_path: Path) -> None:
    import databricks.sdk

    import apx_agent._defaults as defaults
    import apx_agent._dev as dev
    import apx_agent._wiring as wiring
    from apx_agent._doctor_durable import check_durable_readiness

    before = (databricks.sdk.WorkspaceClient, defaults.WorkspaceClient, defaults._make_workspace_client,
              wiring._make_workspace_client, dev.WorkspaceClient)
    check_durable_readiness(_project(tmp_path, "restore", LLM_AGENT.format(name="restore")))
    after = (databricks.sdk.WorkspaceClient, defaults.WorkspaceClient, defaults._make_workspace_client,
             wiring._make_workspace_client, dev.WorkspaceClient)
    assert after == before


def test_system_exit_at_import_is_a_config_fail(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    checks = _by_name(check_durable_readiness(_project(tmp_path, "exits", "raise SystemExit(3)\n")))
    assert checks["Config"].status is Status.FAIL
    assert "SystemExit" in checks["Config"].detail
    for stage in ("Compile", "Startup", "Request", "Deploy"):
        assert checks[stage].status is Status.SKIP


def test_keyboard_interrupt_still_propagates(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    with pytest.raises(KeyboardInterrupt):
        check_durable_readiness(_project(tmp_path, "ctrlc", "raise KeyboardInterrupt()\n"))


def test_malformed_pyproject_is_a_config_fail(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = tmp_path / "badtoml"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project\nname = \n")
    checks = _by_name(check_durable_readiness(root))
    assert checks["Config"].status is Status.FAIL
    assert "TOMLDecodeError" in checks["Config"].detail
    assert checks["Compile"].detail == "blocked by Config"


def test_missing_pyproject_is_a_config_fail(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = tmp_path / "nopyproject"
    root.mkdir()
    checks = _by_name(check_durable_readiness(root))
    assert checks["Config"].status is Status.FAIL
    assert "FileNotFoundError" in checks["Config"].detail


def test_doctor_json_has_durable_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from click.testing import CliRunner

    from apx_agent.cli import main as cli

    root = _project(tmp_path, "jsonproj", LLM_AGENT.format(name="jsonproj"))
    monkeypatch.chdir(root)
    result = CliRunner().invoke(cli, ["doctor", "--offline", "--json", "--durable"])
    payload = json.loads(result.output)
    names = [c["name"] for c in payload["Durable readiness"]]
    assert names[:5] == ["Config", "Compile", "Startup", "Request", "Deploy"]
    assert {c["status"] for c in payload["Durable readiness"]} <= {s.value for s in Status}


DELEGATE_AGENT = (
    "from apx_agent import LlmAgent\n"
    "from apx_agent._agent_tool import remote_agent_tool\n"
    "peer = remote_agent_tool('https://apx-doctor-peer.invalid', name='ask_peer', description='Ask the peer.')\n"
    "agent = LlmAgent(name='{name}', tools=[peer])\n"
)


def test_good_project_passes_stages_0_to_3(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = _project(tmp_path, "good", DELEGATE_AGENT.format(name="good"))
    checks = _by_name(check_durable_readiness(root))
    for stage in ("Config", "Compile", "Startup", "Request"):
        assert checks[stage].status is Status.OK, (stage, checks[stage].detail)
    assert "forwards caller token" in checks["Request"].detail
    assert "local forwards none" in checks["Request"].detail
    assert "DATABRICKS_APP_NAME" not in os.environ


def test_mcp_mount_goes_live_on_durable(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    entry = (
        "from apx_agent import mount_mcp_endpoints\n"
        "from apx_agent._serve import create_app\n"
        "from agent import agent\n"
        "app = create_app()\n"
        "mount_mcp_endpoints(app, agent)\n"
    )
    root = _project(tmp_path, "mcpproj", LLM_AGENT.format(name="mcpproj"), extra_files={"app.py": entry})
    checks = _by_name(check_durable_readiness(root))
    assert checks["Startup"].status is Status.OK, checks["Startup"].detail
    assert "/mcp live" in checks["Startup"].detail


def test_startup_exception_is_a_startup_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("lifespan exploded")

    monkeypatch.setattr("apx_agent._runtime_targets.compile_agent", explode)
    root = _project(tmp_path, "boom", LLM_AGENT.format(name="boom"))
    checks = _by_name(check_durable_readiness(root))
    assert checks["Startup"].status is Status.FAIL
    assert "lifespan exploded" in checks["Startup"].detail
    assert checks["Request"].detail == "blocked by Startup"


def test_declared_sub_agent_card_fetch_is_refused_offline(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    agent_py = "from apx_agent import LlmAgent\nagent = LlmAgent(name='orch', sub_agents=['https://elsewhere.example.com'])\n"
    checks = _by_name(check_durable_readiness(_project(tmp_path, "orch", agent_py)))
    assert checks["Startup"].status is Status.OK, checks["Startup"].detail


def test_saved_invocation_error_is_a_request_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import _doctor_durable
    from apx_agent._doctor_durable import check_durable_readiness

    real = _doctor_durable._stage_request

    class _Missing:
        def __init__(self, client: Any) -> None:
            self._client = client

        def post(self, *args: Any, **kwargs: Any) -> Any:
            return self._client.post(*args, **kwargs)

        def get(self, *args: Any, **kwargs: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(status_code=404, text="not found")

    def wrapped(loaded: Any, started: Any, harness: Any) -> Any:
        started.client = _Missing(started.client)
        return real(loaded, started, harness)

    monkeypatch.setattr(_doctor_durable, "_stage_request", wrapped)
    root = _project(tmp_path, "gone", DELEGATE_AGENT.format(name="gone"))
    request = _by_name(check_durable_readiness(root))["Request"]
    assert request.status is Status.FAIL
    assert "404" in request.detail


def test_entrypoint_syntax_error_is_a_startup_fail(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = _project(tmp_path, "badapp", LLM_AGENT.format(name="badapp"), extra_files={"app.py": "def (:\n"})
    checks = _by_name(check_durable_readiness(root))
    assert checks["Startup"].status is Status.FAIL
    assert "SyntaxError" in checks["Startup"].detail
    assert checks["Request"].status is Status.SKIP


@pytest.mark.parametrize("bundle", ["", "resources:\n  apps:\n    a:\n      config:\n        command: app:app\n"])
def test_odd_databricks_yml_does_not_crash(tmp_path: Path, bundle: str) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = _project(tmp_path, "odd", LLM_AGENT.format(name="odd"), extra_files={"databricks.yml": bundle})
    assert _by_name(check_durable_readiness(root))["Startup"].status is Status.OK


def test_wrong_forward_decision_fails_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import _resources, _runtime_targets
    from apx_agent._doctor_durable import check_durable_readiness

    real_compile = _runtime_targets.compile_agent

    def compile_blind_to_delegates(*args: Any, **kwargs: Any) -> Any:
        # Simulates durable's has_delegates (hence forward) going wrong: while the
        # handlers compile, the agent appears to have no delegating tools.
        with monkeypatch.context() as m:
            m.setattr(_resources, "_iter_tool_fns", lambda agent: [])
            return real_compile(*args, **kwargs)

    monkeypatch.setattr(_runtime_targets, "compile_agent", compile_blind_to_delegates)
    root = _project(tmp_path, "nofwd", DELEGATE_AGENT.format(name="nofwd"))
    request = _by_name(check_durable_readiness(root))["Request"]
    assert request.status is Status.FAIL
    assert "request-user resolver" in request.detail


def test_isolation_leaves_no_mlflow_db_or_error_noise(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    from mlflow.tracing.provider import is_tracing_enabled

    from apx_agent._doctor_durable import check_durable_readiness

    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)
    before = is_tracing_enabled()
    root = _project(tmp_path, "quiet", DELEGATE_AGENT.format(name="quiet"))
    with caplog.at_level(logging.WARNING):
        checks = _by_name(check_durable_readiness(root))
    assert checks["Request"].status is Status.OK, checks["Request"].detail
    assert not list(here.rglob("mlflow.db")) and not list(root.rglob("mlflow.db"))
    assert not [r for r in caplog.records if "mlflow" in r.name.lower() and r.levelno >= logging.WARNING]
    assert is_tracing_enabled() is before


BUNDLE = """\
bundle:
  name: {app}
resources:
  apps:
    {app}:
      name: {app}
{extra}      source_code_path: ./.build
"""


def _bundle_project(tmp_path: Path, name: str, app: str, extra: str) -> Path:
    root = _project(tmp_path, name, LLM_AGENT.format(name=name))
    (root / "databricks.yml").write_text(BUNDLE.format(app=app, extra=extra))
    return root


def test_no_bundle_skips_deploy(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    checks = _by_name(check_durable_readiness(_project(tmp_path, "nobundle", LLM_AGENT.format(name="nobundle"))))
    assert checks["Deploy"].status is Status.SKIP
    assert "generates its own bundle" in checks["Deploy"].detail


def test_clean_bundle_passes_deploy(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    checks = _by_name(check_durable_readiness(_bundle_project(tmp_path, "okbundle", "agent-bricks-ok", "")))
    assert checks["Deploy"].status is Status.OK


@pytest.mark.parametrize(("app", "extra", "needle"), [
    ("mcp-thing", "", "agent-bricks-"),
    ("agent-bricks-thing", "      description: x\n", "description"),
    ("agent-bricks-thing2", "      config:\n        env:\n          - name: MLFLOW_EXPERIMENT_ID\n            value: '1'\n", "MLFLOW_EXPERIMENT_ID"),
])
def test_deploy_preflight_rejections(tmp_path: Path, app: str, extra: str, needle: str) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    checks = _by_name(check_durable_readiness(_bundle_project(tmp_path, app.replace("-", "_"), app, extra)))
    assert checks["Deploy"].status is Status.FAIL
    assert needle in checks["Deploy"].detail


def test_custom_surface_warns_remount(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    entry = (
        "from fastapi import APIRouter\n"
        "from fastapi.middleware.cors import CORSMiddleware\n"
        "from apx_agent import create_app\n"
        "from agent import agent\n"
        "router = APIRouter()\n"
        "app = create_app(agent)\n"
        "app.include_router(router)\n"
        "app.add_middleware(CORSMiddleware, allow_origins=['*'])\n"
    )
    root = _project(tmp_path, "custom", LLM_AGENT.format(name="custom"), extra_files={"app.py": entry})
    checks = _by_name(check_durable_readiness(root))
    assert checks["Re-mount"].status is Status.WARN
    assert "include_router(router)" in checks["Re-mount"].detail
    assert "add_middleware(CORSMiddleware)" in checks["Re-mount"].detail


def test_non_managed_session_warns_history(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = _project(tmp_path, "hist", LLM_AGENT.format(name="hist"))
    (root / "pyproject.toml").write_text(PYPROJECT.format(name="hist") + '\n[tool.apx.agent.memory]\ntype = "inmemory"\n')
    checks = _by_name(check_durable_readiness(root))
    assert checks["History"].status is Status.WARN
    assert "inmemory" in checks["History"].detail


def test_advisories_run_when_config_fails(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = _project(tmp_path, "cfgfail", "raise RuntimeError('boom')\n", extra_files={"app.py": "def (:\n"})
    checks = _by_name(check_durable_readiness(root))
    assert checks["Config"].status is Status.FAIL
    assert checks["Re-mount"].status is Status.WARN
    assert "could not scan entrypoint" in checks["Re-mount"].detail
    assert checks["History"].status is Status.SKIP


@pytest.mark.parametrize("bundle", ["", "bundle:\n  name: x\n", "resources:\n  apps: {}\n", "resources:\n  apps:\n"])
def test_malformed_bundle_is_a_clear_deploy_fail(tmp_path: Path, bundle: str) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = _project(tmp_path, "malformed", LLM_AGENT.format(name="malformed"), extra_files={"databricks.yml": bundle})
    deploy = _by_name(check_durable_readiness(root))["Deploy"]
    assert deploy.status is Status.FAIL
    assert deploy.detail == "databricks.yml has no resources.apps entry"


def test_delegate_agent_with_no_resolver_call_fails_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from databricks_agentkit.runtime.auth import RequestAuthContext

    from apx_agent._doctor_durable import check_durable_readiness

    # With no request-user client, durable skips building headers entirely: the
    # agent has a delegate but the resolver is never installed.
    monkeypatch.setattr(RequestAuthContext, "client_for", lambda self, kind: None)
    root = _project(tmp_path, "noresolver", DELEGATE_AGENT.format(name="noresolver"))
    request = _by_name(check_durable_readiness(root))["Request"]
    assert request.status is Status.FAIL
    assert "no call" in request.detail


def test_delegate_less_agent_request_ok_without_resolver_call(tmp_path: Path) -> None:
    from apx_agent._doctor_durable import check_durable_readiness

    root = _project(tmp_path, "nodelegate", LLM_AGENT.format(name="nodelegate"))
    request = _by_name(check_durable_readiness(root))["Request"]
    assert request.status is Status.OK
    assert "0 delegate(s)" in request.detail
