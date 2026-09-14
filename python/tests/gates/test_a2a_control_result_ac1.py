"""AC-1: a remote loop body calling finish_loop terminates the loop like local."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from tests.gates._a2a_control_helpers import (
    build_loop,
    finish_loop_control,
    patch_remote_replies,
    reply,
    run,
)


def _last_tool_names(state: dict) -> list[str]:
    for message in reversed(state["messages"]):
        if isinstance(message, AIMessage) and message.tool_calls:
            return [tc["name"] for tc in message.tool_calls]
    return []


def test_remote_finish_loop_terminates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = patch_remote_replies(
        monkeypatch, [reply("done", finish_loop_control())]
    )
    graph = build_loop(monkeypatch, tmp_path, max_iterations=5)

    state = run(graph)

    # Terminated on the first iteration — the peer's finish_loop was routed
    # exactly as a local loop body's sentinel, not after exhausting max_iter.
    assert len(calls) == 1
    assert "finish_loop" in _last_tool_names(state)
