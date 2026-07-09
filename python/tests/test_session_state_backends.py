"""Construction-level tests for set_session_state on the Lakebase backend —
assert the UPDATE statement + params are built correctly without a live warehouse."""
import json
from unittest.mock import MagicMock, patch

import pytest


def test_lakebase_set_session_state_builds_update(monkeypatch):
    from apx_agent import _conversation_lakebase as mod

    store = mod.LakebaseConversationStore.__new__(mod.LakebaseConversationStore)
    store._conv_table = "conversations"
    store._tables_created = True  # set_session_state now calls _ensure_tables() (#381)

    conn = MagicMock()
    engine_ctx = MagicMock()
    engine_ctx.__enter__ = MagicMock(return_value=conn)
    engine_ctx.__exit__ = MagicMock(return_value=False)
    store.engine = MagicMock()
    store.engine.begin = MagicMock(return_value=engine_ctx)

    store.set_session_state("c1", {"account_id": "ACME-42"})

    assert conn.execute.called
    args, kwargs = conn.execute.call_args
    # second positional arg is the params dict
    params = args[1]
    assert params["cid"] == "c1"
    assert json.loads(params["ss"]) == {"account_id": "ACME-42"}
    # updated_at is stamped (mirrors update_conversation idiom)
    assert "now" in params
