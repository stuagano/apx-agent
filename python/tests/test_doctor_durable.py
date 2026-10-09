"""`doctor --durable` runs the project offline and reports per-stage readiness."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

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
