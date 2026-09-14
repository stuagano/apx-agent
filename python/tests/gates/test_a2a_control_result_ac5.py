"""AC-5: a no-control remote leaf behaves exactly as before (back-compat).

Gate for prd_a2a-control-result.md AC-5 — regenerate from the PRD, don't hand-edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from tests.gates._a2a_control_helpers import (
    build_sequential,
    patch_remote_replies,
    reply,
    run,
)


def test_no_control_reply_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A sequential remote leaf that returns no ControlSignal must behave as the
    # prior sequential path: its text is the answer, no sentinel is fabricated.
    calls = patch_remote_replies(monkeypatch, [reply("plain answer", None)])
    graph = build_sequential(monkeypatch, tmp_path)

    state = run(graph)

    assert len(calls) == 1
    last = state["messages"][-1]
    assert isinstance(last, AIMessage)
    assert last.content == "plain answer"
    # No control field ⇒ no reconstructed tool_call (NFR-1).
    assert not last.tool_calls
