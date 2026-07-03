"""Claim-vs-reality: set_session_state must self-create tables like siblings (#381).

set_session_state was the only write method that omitted `_ensure_tables()`. On a
fresh auto_create store it therefore raised "relation apx_conversations does not
exist" when it ran before any create_conversation/append — e.g. a resume/approval
flow that restores state first — instead of self-creating the table like every
sibling write.

This reconciles the *claim* ("writes bootstrap their tables on first use")
against reality: set_session_state calls _ensure_tables() before its UPDATE.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apx_agent import LakebaseConversationStore


@pytest.mark.unit
def test_set_session_state_ensures_tables_first() -> None:
    store = LakebaseConversationStore(engine=MagicMock(name="engine"), auto_create=False)
    called = {"ensure": False}
    orig = store._ensure_tables

    def _spy() -> None:
        called["ensure"] = True
        orig()

    store._ensure_tables = _spy  # type: ignore[method-assign]

    # engine.begin() is a mock context manager; the UPDATE is a no-op on it.
    store.set_session_state("c1", {"k": "v"})

    assert called["ensure"], "set_session_state must call _ensure_tables() before its UPDATE"
