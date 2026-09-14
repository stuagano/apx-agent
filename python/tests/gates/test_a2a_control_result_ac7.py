"""AC-7: a control signal and its content coexist on the same reply.

Gate for prd_a2a-control-result.md AC-7 — regenerate from the PRD, don't hand-edit.
"""

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


def test_control_and_content_coexist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A peer that both answers AND signals finish_loop: routing must use the
    # control signal (loop ends on iteration 1) while the answer text remains
    # available in state on the same AIMessage (FR-6, no loss).
    calls = patch_remote_replies(
        monkeypatch, [reply("here is the answer", finish_loop_control())]
    )
    graph = build_loop(monkeypatch, tmp_path, max_iterations=5)

    state = run(graph)

    assert len(calls) == 1  # control drove termination, not max_iterations
    control_msg = next(
        m
        for m in reversed(state["messages"])
        if isinstance(m, AIMessage) and m.tool_calls
    )
    assert "finish_loop" in [tc["name"] for tc in control_msg.tool_calls]
    # Content preserved alongside the control sentinel on the same message.
    assert control_msg.content == "here is the answer"
