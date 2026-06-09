"""Tests for _session.py and _session_delta.py.

Covers:
  - InMemorySessionStore get/put/delete semantics, including isolation of
    stored vs in-flight Session objects.
  - load_or_create_session creates on first access, returns existing on
    subsequent.
  - append_turn appends new messages, doesn't double-append already-present
    ones (by object identity).
  - DeltaSessionStore happy paths: get on missing returns None, put issues
    a MERGE, get parses JSON columns, delete issues DELETE.
  - DeltaSessionStore failure modes: get raises StoreError on SQL
    exception (a backend failure is not a missing session), malformed
    JSON returns empty session.
  - DeltaSessionStore validates the three-part table path.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from apx_agent import (
    DeltaSessionStore,
    InMemorySessionStore,
    Session,
    append_turn,
    load_or_create_session,
)
from apx_agent._session import StoreError


# ===========================================================================
# Session dataclass
# ===========================================================================


def test_session_defaults() -> None:
    s = Session(session_id="x")
    assert s.session_id == "x"
    assert s.history == []
    assert s.state == {}
    assert s.created_at > 0
    assert s.updated_at > 0


# ===========================================================================
# InMemorySessionStore
# ===========================================================================


def test_in_memory_get_missing_returns_none() -> None:
    store = InMemorySessionStore()
    assert store.get("nope") is None


def test_in_memory_put_then_get() -> None:
    store = InMemorySessionStore()
    s = Session(session_id="x", history=[{"role": "user", "content": "hi"}])
    store.put(s)
    got = store.get("x")
    assert got is not None
    assert got.session_id == "x"
    assert got.history == [{"role": "user", "content": "hi"}]


def test_in_memory_put_isolates_stored_copy() -> None:
    """Mutating the session after put should not change the stored value."""
    store = InMemorySessionStore()
    s = Session(session_id="x", history=[{"role": "user", "content": "hi"}])
    store.put(s)
    # Mutate the original
    s.history.append({"role": "assistant", "content": "hello"})
    s.state["foo"] = "bar"
    # Stored copy is unchanged
    got = store.get("x")
    assert got is not None
    assert got.history == [{"role": "user", "content": "hi"}]
    assert got.state == {}


def test_in_memory_put_updates_updated_at() -> None:
    store = InMemorySessionStore()
    s = Session(session_id="x", updated_at=0.0)
    store.put(s)
    got = store.get("x")
    assert got is not None
    assert got.updated_at > 0


def test_in_memory_delete() -> None:
    store = InMemorySessionStore()
    store.put(Session(session_id="x"))
    store.delete("x")
    assert store.get("x") is None
    # Delete missing is a no-op
    store.delete("not-there")


# ===========================================================================
# load_or_create_session
# ===========================================================================


def test_load_or_create_creates_on_first_access() -> None:
    store = InMemorySessionStore()
    s = load_or_create_session(store, "x")
    assert s.session_id == "x"
    assert s.history == []
    # And it's persisted
    assert store.get("x") is not None


def test_load_or_create_returns_existing() -> None:
    store = InMemorySessionStore()
    original = Session(session_id="x", history=[{"role": "user", "content": "hi"}])
    store.put(original)
    got = load_or_create_session(store, "x")
    assert got.history == [{"role": "user", "content": "hi"}]


# ===========================================================================
# append_turn
# ===========================================================================


def test_append_turn_appends_new_messages() -> None:
    s = Session(session_id="x")
    in_msg = {"role": "user", "content": "hi"}
    out_msg = {"role": "assistant", "content": "hello"}
    append_turn(s, input_messages=[in_msg], new_messages=[out_msg])
    assert s.history == [in_msg, out_msg]


def test_append_turn_skips_already_present_inputs_by_identity() -> None:
    """If the session already has an entry (by identity), don't double-add it."""
    in_msg = {"role": "user", "content": "hi"}
    s = Session(session_id="x", history=[in_msg])
    out_msg = {"role": "assistant", "content": "hello"}
    append_turn(s, input_messages=[in_msg], new_messages=[out_msg])
    # in_msg shouldn't appear twice
    assert s.history == [in_msg, out_msg]


# ===========================================================================
# DeltaSessionStore — argument validation
# ===========================================================================


def test_delta_rejects_non_three_part_path() -> None:
    with pytest.raises(ValueError, match="three-part UC name"):
        DeltaSessionStore(table_path="main.sessions", ws=MagicMock())


# ===========================================================================
# DeltaSessionStore — get
# ===========================================================================


def test_delta_get_missing_returns_none() -> None:
    store = DeltaSessionStore(table_path="main.x.sessions", ws=MagicMock(), auto_create=False)
    with patch("apx_agent._session_delta.run_sql", return_value=[]):
        assert store.get("nope") is None


def test_delta_get_parses_history_and_state_json() -> None:
    store = DeltaSessionStore(table_path="main.x.sessions", ws=MagicMock(), auto_create=False)
    row = {
        "session_id": "x",
        "history": json.dumps([{"role": "user", "content": "hi"}]),
        "state": json.dumps({"prefs": {"language": "en"}}),
        "created_at": 1700000000.0,
        "updated_at": 1700000100.0,
    }
    with patch("apx_agent._session_delta.run_sql", return_value=[row]):
        s = store.get("x")
    assert s is not None
    assert s.history == [{"role": "user", "content": "hi"}]
    assert s.state == {"prefs": {"language": "en"}}
    assert s.created_at == 1700000000.0
    assert s.updated_at == 1700000100.0


def test_delta_get_raises_store_error_on_sql_exception() -> None:
    """A backend failure (not a missing session) raises StoreError so callers
    don't mistake an errored read for an empty conversation and clobber it."""
    store = DeltaSessionStore(table_path="main.x.sessions", ws=MagicMock(), auto_create=False)
    with patch("apx_agent._session_delta.run_sql", side_effect=RuntimeError("boom")):
        with pytest.raises(StoreError):
            store.get("x")


def test_delta_get_handles_malformed_json() -> None:
    store = DeltaSessionStore(table_path="main.x.sessions", ws=MagicMock(), auto_create=False)
    row = {
        "session_id": "x",
        "history": "not-json[",
        "state": "{",
        "created_at": 0.0,
        "updated_at": 0.0,
    }
    with patch("apx_agent._session_delta.run_sql", return_value=[row]):
        s = store.get("x")
    assert s is not None
    assert s.history == []
    assert s.state == {}


# ===========================================================================
# DeltaSessionStore — put / delete
# ===========================================================================


def test_delta_put_issues_merge() -> None:
    store = DeltaSessionStore(table_path="main.x.sessions", ws=MagicMock(), auto_create=False)
    session = Session(
        session_id="x",
        history=[{"role": "user", "content": "hello"}],
        state={"prefs": {"language": "en"}},
    )
    with patch("apx_agent._session_delta.run_sql") as mock_run:
        store.put(session)
    assert mock_run.call_count == 1
    sql = mock_run.call_args.args[1]
    assert "MERGE INTO main.x.sessions" in sql
    assert "WHEN MATCHED" in sql
    assert "WHEN NOT MATCHED" in sql


def test_delta_put_serialises_json_and_escapes_single_quotes() -> None:
    store = DeltaSessionStore(table_path="main.x.sessions", ws=MagicMock(), auto_create=False)
    session = Session(
        session_id="user's session",  # apostrophe to test escaping
        history=[{"role": "user", "content": "don't worry"}],
        state={"key": "O'Hara"},
    )
    with patch("apx_agent._session_delta.run_sql") as mock_run:
        store.put(session)
    sql = mock_run.call_args.args[1]
    # All single quotes inside the JSON payload should be doubled
    assert "user''s session" in sql
    assert "don''t worry" in sql
    assert "O''Hara" in sql


def test_delta_delete_issues_delete_sql() -> None:
    store = DeltaSessionStore(table_path="main.x.sessions", ws=MagicMock(), auto_create=False)
    with patch("apx_agent._session_delta.run_sql") as mock_run:
        store.delete("x")
    sql = mock_run.call_args.args[1]
    assert sql.startswith("DELETE FROM main.x.sessions")
    assert "session_id = 'x'" in sql


# ===========================================================================
# DeltaSessionStore — auto-create
# ===========================================================================


def test_delta_auto_create_runs_create_table_on_first_call() -> None:
    store = DeltaSessionStore(table_path="main.x.sessions", ws=MagicMock())
    with patch("apx_agent._session_delta.run_sql", return_value=[]) as mock_run:
        store.get("x")
    create_sqls = [c.args[1] for c in mock_run.call_args_list if "CREATE TABLE" in c.args[1]]
    assert len(create_sqls) == 1
    assert "CREATE TABLE IF NOT EXISTS main.x.sessions" in create_sqls[0]


def test_delta_auto_create_runs_only_once() -> None:
    store = DeltaSessionStore(table_path="main.x.sessions", ws=MagicMock())
    with patch("apx_agent._session_delta.run_sql", return_value=[]) as mock_run:
        store.get("x")
        store.get("y")
        store.delete("z")
    create_sqls = [c for c in mock_run.call_args_list if "CREATE TABLE" in c.args[1]]
    assert len(create_sqls) == 1


def test_delta_no_auto_create_when_disabled() -> None:
    store = DeltaSessionStore(table_path="main.x.sessions", ws=MagicMock(), auto_create=False)
    with patch("apx_agent._session_delta.run_sql", return_value=[]) as mock_run:
        store.get("x")
    create_sqls = [c for c in mock_run.call_args_list if "CREATE TABLE" in c.args[1]]
    assert create_sqls == []


def test_delta_ensure_table_retries_after_create_failure() -> None:
    """CREATE TABLE failure must not mark _created=True — next call retries."""
    store = DeltaSessionStore(table_path="main.x.sessions", ws=MagicMock())
    create_calls: list[str] = []

    def flaky_run_sql(ws, sql, **kwargs):
        if "CREATE TABLE" in sql:
            create_calls.append(sql)
            raise RuntimeError("PERMISSION_DENIED")
        return []

    with patch("apx_agent._session_delta.run_sql", side_effect=flaky_run_sql):
        store._ensure_table()
        assert not store._created, "_created must stay False after CREATE TABLE failure"
        store._ensure_table()  # second call should retry CREATE TABLE
    assert len(create_calls) == 2, "Expected two CREATE TABLE attempts"


def test_delta_put_raises_store_error_on_sql_failure() -> None:
    """put() wraps run_sql errors as StoreError with a GRANT hint."""
    store = DeltaSessionStore(table_path="main.x.sessions", ws=MagicMock(), auto_create=False)
    session = Session(session_id="s1", history=[], state={})

    def fail_merge(ws, sql, **kwargs):
        if "MERGE INTO" in sql:
            raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND: main.x.sessions")
        return []

    with patch("apx_agent._session_delta.run_sql", side_effect=fail_merge):
        with pytest.raises(StoreError, match="GRANT"):
            store.put(session)


def test_persist_session_degrades_gracefully_on_store_error(caplog) -> None:
    """_persist_session catches StoreError and logs a warning instead of crashing."""
    import logging
    from apx_agent._responses_agent import _persist_session
    from apx_agent._session import Session, StoreError

    store = MagicMock()
    store.put.side_effect = StoreError("no perms")
    session = Session(session_id="s1", history=[], state={})

    with caplog.at_level(logging.WARNING):
        _persist_session(store, session, input_items=[], output_items=[])

    assert any("degraded to ephemeral" in r.message for r in caplog.records)
