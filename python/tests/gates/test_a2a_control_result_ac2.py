"""AC-2: a remote loop body with no finish_loop iterates like local continue."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.gates._a2a_control_helpers import (
    build_loop,
    patch_remote_replies,
    reply,
    run,
)


def test_remote_loop_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No control signal ever ⇒ keep iterating until max_iterations, exactly as
    # a local loop body that never calls finish_loop.
    calls = patch_remote_replies(monkeypatch, [reply("keep going", None)])
    graph = build_loop(monkeypatch, tmp_path, max_iterations=3)

    run(graph)

    assert len(calls) == 3
