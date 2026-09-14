"""AC-6: both _wiring.py guards removed; remote loop/handoff bindings resolve.

Gate for prd_a2a-control-result.md AC-6 — regenerate from the PRD, don't hand-edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.gates._a2a_control_helpers import (
    build_handoff,
    build_loop,
    patch_remote_replies,
    reply,
    run,
)


def test_remote_loop_handoff_bindings_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With the two guards at _wiring.py:492-497 removed, compiling a remote loop
    # body and a remote handoff peer must resolve without raising ValueError —
    # exactly the path that used to be rejected. Compilation IS the guard test.
    patch_remote_replies(monkeypatch, [reply("ok", None)])

    loop = build_loop(monkeypatch, tmp_path, max_iterations=1)
    handoff = build_handoff(monkeypatch, tmp_path)

    assert callable(loop.invoke)
    assert callable(handoff.invoke)

    # Resolved bindings actually run (no residual guard on the execution path).
    run(loop)
    run(handoff)
