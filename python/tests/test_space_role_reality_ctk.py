"""Claim-vs-reality: a role-required App Space is visible where operators read identity (#1070).

A role-required space acts with the role's permissions, not the calling user's,
so per-hop user OBO does not hold there. Deploy refuses user-identity tools and
hops in such a space; these tests read back the two surfaces that report it:
the ``doctor --json`` output and the deploy authorization summary (identity plan).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from ctk import Artifact, verify
from databricks.sdk.service.apps import Space

import apx_agent._doctor as doctor
from apx_agent import Agent, AgentConfig, tool
from apx_agent._apps_authorization import AppFamilyPermissions, authorization_summary_lines, compile_authorization_plan


@tool(execution="user")
def lookup() -> str:
    return "ok"


def test_doctor_json_names_the_role_and_the_user_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent.cli import main

    ws = MagicMock()
    ws.apps.get_space.return_value = Space(name="agent-space", assume_group_id="analysts")
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda: ws)
    config = AgentConfig(name="native", target="durable_agent_server", deploy={"space": "agent-space"})
    rows = doctor.check_space_prerequisites(tmp_path, config, online=True, agent=Agent(tools=[lookup]))
    monkeypatch.setattr(doctor, "run_checks", lambda *a, **k: [("Project", rows)])

    result = CliRunner().invoke(main, ["doctor", "--json", "--offline"])
    out = tmp_path / "doctor.json"
    out.write_text(result.output)
    assert result.exit_code == 1  # a FAIL row fails doctor
    verify(
        Artifact(str(out), min_bytes=100, is_json=True, json_keys=["Project"], must_contain='"name": "App Space role"'),
        Artifact(str(out), must_contain="requires the role of group 'analysts'"),
        Artifact(str(out), must_contain="  - tool 'lookup'"),
    )


def test_deploy_summary_shows_the_space_role(tmp_path: Path) -> None:
    @tool(execution="service")
    def background() -> str:
        return "ok"

    plan = compile_authorization_plan(Agent(tools=[background]), model="databricks-claude-sonnet-4-6")
    out = tmp_path / "summary.txt"
    out.write_text("\n".join(authorization_summary_lines(
        dataclasses.replace(plan, space_role="analysts"), [], AppFamilyPermissions())))
    verify(Artifact(str(out), min_bytes=100, must_contain=(
        "App Space role:\n  - group 'analysts': users act with this role's permissions; "
        "no tools or sub-agent hops run as the user")))
    # No role, no section: a dedicated-app summary is unchanged.
    out.write_text("\n".join(authorization_summary_lines(plan, [], AppFamilyPermissions())))
    verify(Artifact(str(out), min_bytes=100, must_not_contain="App Space role"))
