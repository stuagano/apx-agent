"""Conformance (#1133, ADR 0001): every generated durable project passes the real `agentbricks doctor`.

Doctor only reads files (no imports, no network), so this runs in credential-free CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from ctk import Artifact, verify

from apx_agent._models import AgentConfig
from apx_agent._project_gen import generate_project

TEMPLATES: dict[str, dict[str, Any] | None] = {
    "base": None,
    "data": {"name": "data", "catalog": "main", "schema": "sales"},
    "coworker": {"name": "coworker", "catalog": "main", "schema": "hr", "persona": "an analyst"},
}
CHECKS = {
    "agent_manifest", "agentbricks_server", "project_metadata", "agentbricks_dependency",
    "app_command", "agent_app", "framework_adapter", "deployment_name",
}


def _generate(root: Path, template: str, *, scaffold: bool = False) -> Path:
    project = root / template
    if scaffold:  # `apx-agent agents scaffold`, the other path that writes a durable project
        from apx_agent.cli import _scaffold_apps

        _scaffold_apps(project, template, False, "main", "sales", template=template, ci=None,
                       runtime="durable_agent_server")
    else:
        generate_project(AgentConfig(name=template, target="durable_agent_server",
                                     template=TEMPLATES[template]), project)
    return project


def _doctor(project: Path) -> dict[str, Any]:
    """Run the installed `agentbricks doctor` console script; its JSON report plus the exit code."""
    cli = Path(sys.executable).parent / "agentbricks"
    proc = subprocess.run([str(cli), "--output", "json", "doctor", str(project)], capture_output=True,
                          text=True, env={**os.environ, "AGENTBRICKS_DISABLE_AGENT_HINT": "1"}, check=False)
    report = json.loads(proc.stdout)
    report["exit_code"] = proc.returncode
    report["statuses"] = {c["id"]: c["status"] for c in report["checks"]}
    return report


@pytest.mark.parametrize("scaffold", [False, True], ids=["generate_project", "scaffold"])
@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_generated_durable_project_passes_agentbricks_doctor(tmp_path: Path, template: str, scaffold: bool) -> None:
    project = _generate(tmp_path, template, scaffold=scaffold)
    report = _doctor(project)
    checks = report["statuses"]
    assert set(checks) == CHECKS  # a renamed or new upstream check must be looked at, not ignored
    assert report["exit_code"] == 0 and report["onboarded"] is True, checks
    assert set(checks.values()) == {"pass"} and report["framework"] == "langgraph", checks
    verify(Artifact(str(project / "agent.toml"), min_bytes=40, must_contain="schema_version = 1"))
    # Upstream ignores unknown keys, so ADR 0001 decision 2 (no apx keys in agent.toml) is checked here.
    assert set(tomllib.loads((project / "agent.toml").read_text())) <= {
        "schema_version", "agent", "auth", "session_store", "memory_store"}
    verify(Artifact(str(project / "server.py"), min_bytes=200, must_contain="app.invoke(invoke)"))
    verify(Artifact(str(project / "app.yaml"), min_bytes=40, must_contain="server.py"))
    verify(Artifact(str(project / ".agentbricks" / "project.toml"), min_bytes=20, must_contain='template = "apx"'))
    assert not (project / "__pycache__").exists()


@pytest.mark.parametrize("edit, failing", [
    (("agent.toml", '[session_store]\nname = "apx-base-sessions"', "[session_store]\nnam = 1"), "agent_manifest"),
    (("server.py", "    app.invoke(invoke)\n", ""), "agent_app"),
    (("server.py", "lambda store: checkpointer(store)", "None"), "framework_adapter"),
])
def test_doctor_rejects_what_apx_must_not_emit(tmp_path: Path, edit: tuple[str, str, str], failing: str) -> None:
    # The conformance test has teeth: a key agent.toml rejects or an unrecognised entrypoint fails it.
    project = _generate(tmp_path, "base")
    name, old, new = edit
    text = (project / name).read_text()
    assert old in text
    (project / name).write_text(text.replace(old, new, 1))
    report = _doctor(project)
    assert report["exit_code"] != 0 and report["statuses"][failing] == "fail", report["statuses"]


def test_generated_server_builds_the_app_through_agentkit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Static checks aside, server.py really constructs the server, registers invoke and
    # takes its saver from AgentKit's LangGraph checkpointer for the bound store.
    import runpy
    from unittest.mock import MagicMock

    import databricks_agentkit.langgraph as adapter
    from databricks_agentkit import DurableAgentServer
    from langgraph.checkpoint.memory import InMemorySaver

    project = _generate(tmp_path, "base")
    stores: list[str] = []
    saver = InMemorySaver()
    monkeypatch.setattr(adapter, "checkpointer", lambda store: stores.append(store) or saver)
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **_: MagicMock())
    monkeypatch.setenv("AGENT_SESSION_STORE", "apx-base-sessions")
    monkeypatch.setenv("APX_PYPROJECT", str(project / "pyproject.toml"))  # create_app sets it; undo it after
    monkeypatch.chdir(project)
    monkeypatch.syspath_prepend(str(project))
    monkeypatch.delitem(sys.modules, "agent", raising=False)
    app = runpy.run_path(str(project / "server.py"), run_name="server")["app"]
    sys.modules.pop("agent", None)
    assert isinstance(app, DurableAgentServer)
    assert app._invoke_hook is not None
    assert stores == ["apx-base-sessions"]
