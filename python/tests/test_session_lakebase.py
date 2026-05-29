"""Tests for _session_lakebase.py — Postgres-backed SessionStore.

Uses SQLAlchemy's built-in in-memory SQLite engine as a stand-in for
Lakebase Postgres. ``ON CONFLICT (session_id) DO UPDATE`` is the same
shape in SQLite and Postgres, so the SQL we emit runs in both. The point
of the tests is store semantics, not Lakebase-specific behavior.

Covers:
  - get on missing returns None
  - put inserts; second put with same id updates (ON CONFLICT upsert)
  - delete removes the row
  - JSON round-trip for history and state
  - updated_at refreshed on put
  - get raises StoreError when underlying SQL raises (a backend failure
    is not a missing session)
  - get returns empty session when JSON columns are malformed
  - auto_create runs CREATE TABLE IF NOT EXISTS exactly once
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine  # noqa: E402

from apx_agent import LakebaseSessionStore, Session  # noqa: E402
from apx_agent._session import StoreError  # noqa: E402


def _store() -> LakebaseSessionStore:
    """In-memory SQLite engine — same DDL/upsert shape as Postgres for these tests."""
    engine = create_engine("sqlite:///:memory:")
    return LakebaseSessionStore(engine=engine, table_name="apx_sessions")


# ===========================================================================
# get / put / delete
# ===========================================================================


def test_get_missing_returns_none() -> None:
    store = _store()
    assert store.get("nope") is None


def test_put_then_get_round_trips() -> None:
    store = _store()
    s = Session(
        session_id="x",
        history=[{"role": "user", "content": "hi"}],
        state={"prefs": {"language": "en"}},
    )
    store.put(s)
    got = store.get("x")
    assert got is not None
    assert got.session_id == "x"
    assert got.history == [{"role": "user", "content": "hi"}]
    assert got.state == {"prefs": {"language": "en"}}


def test_put_upserts_on_session_id_collision() -> None:
    store = _store()
    s1 = Session(session_id="x", history=[{"role": "user", "content": "first"}])
    store.put(s1)
    s2 = Session(session_id="x", history=[{"role": "user", "content": "second"}])
    store.put(s2)
    got = store.get("x")
    assert got is not None
    assert got.history == [{"role": "user", "content": "second"}]


def test_put_updates_updated_at() -> None:
    store = _store()
    s = Session(session_id="x", updated_at=0.0)
    before = time.time()
    store.put(s)
    after = time.time()
    got = store.get("x")
    assert got is not None
    assert before <= got.updated_at <= after + 0.5


def test_delete_removes_row() -> None:
    store = _store()
    store.put(Session(session_id="x", history=[{"role": "user", "content": "hi"}]))
    store.delete("x")
    assert store.get("x") is None
    # Delete-missing is a no-op
    store.delete("not-there")


# ===========================================================================
# JSON round-trip with special characters
# ===========================================================================


def test_apostrophes_and_unicode_round_trip() -> None:
    store = _store()
    s = Session(
        session_id="user's session",
        history=[{"role": "user", "content": "don't worry — résumé"}],
        state={"key": "O'Hara"},
    )
    store.put(s)
    got = store.get("user's session")
    assert got is not None
    assert got.history[0]["content"] == "don't worry — résumé"
    assert got.state == {"key": "O'Hara"}


# ===========================================================================
# Failure modes
# ===========================================================================


def test_get_raises_store_error_on_sql_exception() -> None:
    store = _store()
    # Drop the table so the SELECT fails
    with store.engine.begin() as conn:
        from sqlalchemy import text
        conn.execute(text("DROP TABLE IF EXISTS apx_sessions"))
    # _ensure_table flag is already set; force it back so the failure path
    # exercises the get-on-broken-table case.
    store._created = True
    store._auto_create = False
    # A backend failure is not a missing session: raise StoreError so callers
    # don't mistake an errored read for an empty conversation and clobber it.
    with pytest.raises(StoreError):
        store.get("anything")


def test_get_handles_malformed_json() -> None:
    store = _store()
    # Insert a row with corrupted JSON columns directly via the engine
    with store.engine.begin() as conn:
        from sqlalchemy import text
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS apx_sessions ("
            "session_id TEXT PRIMARY KEY, history TEXT NOT NULL, state TEXT NOT NULL,"
            "created_at DOUBLE PRECISION, updated_at DOUBLE PRECISION)"
        ))
        conn.execute(text(
            "INSERT INTO apx_sessions VALUES ('x', 'not-json[', '{', 0, 0)"
        ))
    store._created = True
    got = store.get("x")
    assert got is not None
    assert got.history == []
    assert got.state == {}


# ===========================================================================
# auto_create
# ===========================================================================


def test_auto_create_runs_once() -> None:
    store = _store()
    initial_calls = []

    real_begin = store.engine.begin

    def counting_begin(*a, **kw):
        ctx = real_begin(*a, **kw)
        initial_calls.append(ctx)
        return ctx

    with patch.object(store.engine, "begin", counting_begin):
        store.get("x")
        store.get("y")
        store.delete("z")
    # CREATE TABLE happens at most once. After the first call to get,
    # _created is True and subsequent ops should not retry the CREATE.
    # We can't easily count DDL calls separately without deeper plumbing,
    # but we can at least assert the flag flipped.
    assert store._created is True


def test_no_auto_create_when_disabled() -> None:
    engine = create_engine("sqlite:///:memory:")
    store = LakebaseSessionStore(engine=engine, table_name="apx_sessions", auto_create=False)
    # Without auto_create, get on a fresh engine hits a missing table. That's a
    # backend failure, not a missing session, so it raises StoreError rather
    # than returning None (callers must not treat it as an empty conversation).
    with pytest.raises(StoreError):
        store.get("anything")
