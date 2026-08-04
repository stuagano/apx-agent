"""Smoke checks for apx-builder Agent safety wiring."""

from apx_agent import PolicyGate


def test_agent_gates_workspace_writes():
    from app import _WRITE_TOOLS, _write_gate, agent

    assert agent._before_tool is _write_gate
    assert isinstance(_write_gate, PolicyGate)
    assert _WRITE_TOOLS == frozenset({"scaffold_project", "deploy_agent"})
    assert "search_tables" not in _WRITE_TOOLS
    assert "poll_deployment" not in _WRITE_TOOLS
