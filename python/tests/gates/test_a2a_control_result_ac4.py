"""AC-4: transfer_to:<unknown target> is a sanitized protocol error.

Gate for prd_a2a-control-result.md AC-4 — regenerate from the PRD, don't hand-edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.gates._a2a_control_helpers import (
    CARD_URL,
    build_handoff,
    patch_remote_replies,
    reply,
    run,
    transfer_control,
)


def test_unknown_transfer_target_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 'body' names a transfer target that is not a sibling in the local graph.
    # FR-4: this is a protocol error naming the logical binding + target, with
    # no URL/transport leakage and no fabricated successful answer.
    patch_remote_replies(
        monkeypatch,
        [reply("routing", transfer_control("nowhere", context="x"))],
    )
    graph = build_handoff(monkeypatch, tmp_path)

    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - message asserted below
        run(graph)

    message = str(excinfo.value)
    assert "body" in message, message
    assert "nowhere" in message, message
    # NFR-2: no transport/URL/credential detail leaks.
    for secret in (CARD_URL, "pricing.internal", "http://", "https://"):
        assert secret not in message, f"leaked {secret!r} in {message!r}"
