import pytest

from apx_agent._conversation import InMemoryConversationStore
from apx_agent._session_state import persistable_state, persist_session_state


def _store_with_conv(cid: str = "c1") -> InMemoryConversationStore:
    store = InMemoryConversationStore("memory://")
    store.create_conversation(id=cid)
    return store


def test_set_and_read_back_session_state():
    store = _store_with_conv()
    store.set_session_state("c1", {"account_id": "ACME-42", "n": 3})
    conv = store.get_conversation("c1")
    assert conv is not None
    assert conv.session_state == {"account_id": "ACME-42", "n": 3}


def test_set_session_state_full_overwrite():
    store = _store_with_conv()
    store.set_session_state("c1", {"a": 1})
    store.set_session_state("c1", {"b": 2})
    assert store.get_conversation("c1").session_state == {"b": 2}


def test_set_session_state_missing_conv_is_noop():
    store = InMemoryConversationStore("memory://")
    store.set_session_state("nope", {"a": 1})  # must not raise


def test_persistable_state_strips_temp_keys():
    out = persistable_state({"keep": 1, "temp:scratch": 2, "also": 3})
    assert out == {"keep": 1, "also": 3}


def test_persistable_state_handles_none():
    assert persistable_state(None) == {}


def test_persist_session_state_strips_temp_and_writes():
    store = _store_with_conv()
    persist_session_state(store, "c1", {"x": 1, "temp:y": 2})
    assert store.get_conversation("c1").session_state == {"x": 1}


def test_persist_session_state_noops_without_conv_id():
    store = _store_with_conv()
    persist_session_state(store, None, {"x": 1})  # must not raise
    assert store.get_conversation("c1").session_state == {}


def test_persist_session_state_swallows_backend_error():
    class _Boom:
        def set_session_state(self, conversation_id, session_state):
            raise RuntimeError("backend down")

    # Must not raise — degraded, not fatal.
    persist_session_state(_Boom(), "c1", {"x": 1})
