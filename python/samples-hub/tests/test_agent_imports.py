"""Smoke test — keeps CI green on a fresh scaffold."""

from __future__ import annotations


def test_agent_importable() -> None:
    from agent import agent

    assert agent is not None
    assert getattr(agent, "name", None) == "samples-hub"
