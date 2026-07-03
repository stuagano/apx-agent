"""Claim-vs-reality: concurrent appends must not collide on position (#373).

append() computed the next item position as ``MAX(position)+1`` inside a
READ COMMITTED transaction with no row lock and no unique constraint. Two
concurrent appends to the same conversation both read the same MAX and insert at
the same position; since only item_id is unique, no error is raised, and
``ORDER BY position`` then interleaves the turns nondeterministically while
cursor pagination skips/duplicates items.

This reconciles the *claim* ("items get a monotonic per-conversation position")
against reality: append() must serialize per-conversation writers by locking the
conversation row (``SELECT ... FOR UPDATE``) before reading MAX(position), so a
concurrent appender blocks until the first commits and sees the true max.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apx_agent import LakebaseConversationStore


@pytest.mark.unit
def test_append_locks_conversation_row_before_reading_max_position(monkeypatch) -> None:
    store = LakebaseConversationStore(engine=MagicMock(name="engine"), auto_create=False)

    # Isolate the transaction body to just the lock: stub the surrounding helpers.
    monkeypatch.setattr(store, "get_conversation", lambda cid: {"conversation_id": cid})
    monkeypatch.setattr(store, "_next_base_position", lambda *_a: 0)
    monkeypatch.setattr(store, "_insert_items", lambda *_a: [])
    monkeypatch.setattr(store, "_bump_updated_at", lambda *_a: None)

    executed: list[str] = []
    conn = MagicMock(name="conn")
    conn.execute.side_effect = lambda stmt, *_a, **_k: executed.append(str(stmt)) or MagicMock()
    store.engine.begin.return_value.__enter__.return_value = conn
    store.engine.begin.return_value.__exit__.return_value = False

    store.append("c1", [])

    locks = [s for s in executed if "FOR UPDATE" in s.upper()]
    assert locks, "append() did not lock the conversation row FOR UPDATE — concurrent appends can collide"
    assert store._conv_table in locks[0], "the FOR UPDATE lock must target the conversation row"


@pytest.mark.unit
def test_items_ddl_has_unique_position_constraint() -> None:
    store = LakebaseConversationStore(engine=MagicMock(), auto_create=False)
    ddl = store._ddl_items().upper().replace(" ", "")
    assert "UNIQUE(CONVERSATION_ID,POSITION)" in ddl, (
        "new items tables should carry UNIQUE(conversation_id, position) as defense-in-depth"
    )
