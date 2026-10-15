"""Restartable Lakebase-memory import into managed memory. No source writes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from apx_agent._state_migration import (
    MemoryDestination,
    import_memories,
    inventory_memory_table,
)


class _Memory:
    def __init__(self, content: str, principal_id: str) -> None:
        self.content = content
        self.principal_id = principal_id


class _Destination:
    """Records writes. Satisfies MemoryDestination without a workspace client."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, memory: Mapping[str, Any]) -> Any:
        self.rows.append(dict(memory))
        return _Memory(str(memory["content"]), str(memory["principal_id"]))

    def list(self, filter: Any) -> list[Any]:
        return [
            _Memory(str(row["content"]), str(row["principal_id"]))
            for row in self.rows
            if row["principal_id"] == filter.principal_id
        ]


def _engine() -> Any:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE apx_memories ("
                "id TEXT PRIMARY KEY, principal_id TEXT, namespace TEXT, content TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO apx_memories (id, principal_id, namespace, content) VALUES "
                "('m1', 'user:alice', 'prefs', 'likes tea'), "
                "('m2', 'user:alice', 'prefs', 'likes quiet'), "
                "('m3', 'user:bob', 'notes', 'secret')"
            )
        )
    return engine


def test_inventory_counts_without_writing() -> None:
    engine = _engine()
    found = inventory_memory_table(engine, "apx_memories")
    assert found.row_count == 3
    assert found.principals == ("user:alice", "user:bob")
    assert found.namespaces == ("notes", "prefs")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM apx_memories")).scalar_one() == 3


def test_import_is_restartable_and_skips_unmapped_principals() -> None:
    engine = _engine()
    destination = _Destination()
    actor_map = {"user:alice": "alice"}
    first = import_memories(
        engine=engine,
        table_name="apx_memories",
        destination=destination,
        actor_map=actor_map,
    )
    assert (first.source_rows, first.written, first.skipped, first.unmapped) == (
        3,
        2,
        0,
        1,
    )
    assert destination.rows[0]["content"].endswith("[source-id:m1]")
    assert destination.rows[0]["principal_id"] == "alice"
    second = import_memories(
        engine=engine,
        table_name="apx_memories",
        destination=destination,
        actor_map=actor_map,
    )
    assert (second.written, second.skipped, second.unmapped) == (0, 2, 1)
    assert len(destination.rows) == 2
    scoped = import_memories(
        engine=engine,
        table_name="apx_memories",
        destination=destination,
        actor_map={"user:bob": "bob"},
        principal_id="user:alice",
    )
    assert (scoped.source_rows, scoped.written, scoped.unmapped) == (2, 0, 0)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM apx_memories")).scalar_one() == 3


def test_blank_actor_mapping_counts_as_unmapped_and_writes_nothing() -> None:
    engine = _engine()
    destination = _Destination()
    result = import_memories(
        engine=engine,
        table_name="apx_memories",
        destination=destination,
        actor_map={"user:alice": "  "},
        principal_id="user:alice",
    )
    assert (result.source_rows, result.written, result.unmapped) == (2, 0, 2)
    assert destination.rows == []


def test_refuses_to_rerun_when_the_destination_page_is_full() -> None:
    engine = _engine()
    destination = _Destination()
    destination.rows = [
        {
            "principal_id": "alice",
            "content": f"fact\n[source-id:old-{index}]",
        }
        for index in range(1000)
    ]
    with pytest.raises(RuntimeError, match="at least 1000"):
        import_memories(
            engine=engine,
            table_name="apx_memories",
            destination=destination,
            actor_map={"user:alice": "alice"},
            principal_id="user:alice",
        )
    assert len(destination.rows) == 1000


def test_refuses_a_table_name_that_is_not_an_identifier() -> None:
    engine = _engine()
    with pytest.raises(ValueError, match="non-identifier"):
        inventory_memory_table(engine, "apx_memories; drop table apx_memories")


def test_destination_accepts_the_recording_double() -> None:
    destination: MemoryDestination = _Destination()
    assert destination.list(type("F", (), {"principal_id": "alice"})()) == []
