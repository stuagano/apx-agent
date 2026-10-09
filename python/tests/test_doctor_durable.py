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
