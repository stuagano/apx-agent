"""AC-3: a remote handoff peer transferring to a known target routes like local.

Gate for prd_a2a-control-result.md AC-3 — regenerate from the PRD, don't hand-edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from tests.gates._a2a_control_helpers import (
    build_handoff,
    patch_remote_replies,
    reply,
    run,
    transfer_control,
)


def _find_tool_call(state: dict, name: str) -> dict | None:
    for message in state["messages"]:
        if isinstance(message, AIMessage) and message.tool_calls:
            for tc in message.tool_calls:
                if tc["name"] == name:
                    return tc
    return None


def test_remote_handoff_routes_known_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 'body' transfers to the known local sibling 'target' with context; then
    # 'target' answers plainly. Routing must match a local handoff: the peer's
    # transfer_to_<target> ControlSignal is reconstructed into a sentinel
    # tool_call the existing router consumes, control moves to 'target', and the
    # transfer context is preserved on the routed message.
    calls = patch_remote_replies(
        monkeypatch,
        [
            reply("routing now", transfer_control("target", context="need pricing")),
            reply("target answer", None),
        ],
    )
    graph = build_handoff(monkeypatch, tmp_path)

    state = run(graph)

    # Control transferred to the local target: 'target' ran (at least once) after
    # 'body' signalled the transfer — the peer's ControlSignal was reconstructed
    # into a sentinel tool_call the existing handoff router consumed, exactly as
    # for a local transfer. (The router re-reads the last sentinel until
    # max_handoffs, identical to a local handoff whose target just answers.)
    assert len(calls) >= 2
    transfer = _find_tool_call(state, "transfer_to_target")
    assert transfer is not None, "transfer_to_target sentinel not routed"
    # Context delivered: the transfer args carried through to the routed signal.
    assert transfer["args"].get("context") == "need pricing"
    # Target's answer is the terminal content (routing reached the target).
    assert state["messages"][-1].content == "target answer"
