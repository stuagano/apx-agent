"""Inventory and restartable import of Lakebase memory into managed memory.

Issue #859 authorizes planning the rollout, not deployment or deletion. The
inventory only counts source rows. The importer writes destination memories and
never deletes, truncates, or updates a source table. Managed memory stores
content and an actor id, not tags, importance, embeddings, or metadata, so each
imported row appends its source id to the content and drops the rest.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class MemoryDestination(Protocol):
    """The two managed-memory operations an import needs."""

    def add(self, memory: Mapping[str, Any]) -> Any: ...

    def list(self, filter: Any) -> Sequence[Any]: ...


_SOURCE_PREFIX = "source-id:"
_PAGE = 500
_LIST_LIMIT = 1000


@dataclass(frozen=True)
class MemoryTableInventory:
    """Counts and distinct scopes for one Lakebase memory table."""

    table_name: str
    row_count: int
    principals: tuple[str, ...]
    namespaces: tuple[str, ...]


@dataclass(frozen=True)
class MemoryImportResult:
    """Counts for one import pass. ``skipped`` rows were already imported."""

    source_rows: int
    written: int
    skipped: int
    unmapped: int


def inventory_memory_table(engine: Any, table_name: str) -> MemoryTableInventory:
    """Count rows and list principals and namespaces. Does not write."""
    sa = _sqlalchemy()
    quoted = _quoted_ident(table_name)
    with engine.connect() as conn:
        count = conn.execute(
            sa.text(f"SELECT COUNT(*) AS n FROM {quoted}")
        ).scalar_one()
        principals = (
            conn.execute(
                sa.text(
                    f"SELECT DISTINCT principal_id FROM {quoted} ORDER BY principal_id"
                )
            )
            .scalars()
            .all()
        )
        namespaces = (
            conn.execute(
                sa.text(f"SELECT DISTINCT namespace FROM {quoted} ORDER BY namespace")
            )
            .scalars()
            .all()
        )
    return MemoryTableInventory(
        table_name=table_name,
        row_count=int(count),
        principals=tuple(str(value) for value in principals),
        namespaces=tuple(str(value) for value in namespaces),
    )


def import_memories(
    *,
    engine: Any,
    table_name: str,
    destination: MemoryDestination,
    actor_map: Mapping[str, str],
    principal_id: str | None = None,
) -> MemoryImportResult:
    """Copy one principal's memories, or every mapped principal when omitted.

    ``actor_map`` is the reviewed source-principal to destination-actor mapping.
    A source principal with a blank mapped actor is counted as ``unmapped`` and
    not written. A principal absent from the map is also ``unmapped`` when the
    call scans every principal; a call that names ``principal_id`` only counts
    that principal. A row whose content already ends with ``[source-id:<id>]``
    for the destination actor is skipped, so a rerun does not write a second copy.
    """
    sa = _sqlalchemy()
    quoted = _quoted_ident(table_name)
    if principal_id is None:
        actors: Sequence[str] = tuple(actor_map.values())
    else:
        mapped = actor_map.get(principal_id)
        actors = (mapped,) if mapped and mapped.strip() else ()
    existing = _imported_source_ids(destination, actors)
    written = skipped = unmapped = source_rows = 0
    last_id = ""
    principal_clause = (
        " AND principal_id = :principal_id" if principal_id is not None else ""
    )
    while True:
        params: dict[str, Any] = {"last_id": last_id}
        if principal_id is not None:
            params["principal_id"] = principal_id
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        f"SELECT id, principal_id, namespace, content FROM {quoted} "
                        f"WHERE id > :last_id{principal_clause} ORDER BY id LIMIT {_PAGE}"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
        if not rows:
            break
        for row in rows:
            source_rows += 1
            source_id = str(row["id"])
            last_id = source_id
            principal = str(row["principal_id"])
            if principal_id is not None and principal != principal_id:
                continue
            actor = actor_map.get(principal)
            if actor is not None and not actor.strip():
                unmapped += 1
                continue
            if not actor:
                if principal_id is None:
                    unmapped += 1
                continue
            if (actor, source_id) in existing:
                skipped += 1
                continue
            namespace = str(row["namespace"] or "default")
            destination.add(
                {
                    "principal_id": actor,
                    "namespace": namespace,
                    "content": _marked_content(str(row["content"]), source_id),
                }
            )
            existing.add((actor, source_id))
            written += 1
    return MemoryImportResult(
        source_rows=source_rows,
        written=written,
        skipped=skipped,
        unmapped=unmapped,
    )


def _imported_source_ids(
    destination: MemoryDestination,
    actors: Sequence[str],
) -> set[tuple[str, str]]:
    """Read destination content so a rerun can recognize earlier copies.

    The service description is only the first content line, so the source id
    travels as a trailing marker inside content and is read back from there.
    """
    from ._memory import MemoryFilter

    found: set[tuple[str, str]] = set()
    for actor in dict.fromkeys(actor for actor in actors if actor and actor.strip()):
        entries = list(
            destination.list(MemoryFilter(principal_id=actor, limit=_LIST_LIMIT))
        )
        # list() has no cursor. A full page may hide earlier copies, so refuse
        # rather than write them again.
        if len(entries) >= _LIST_LIMIT:
            raise RuntimeError(
                f"Destination already holds at least {_LIST_LIMIT} memories for "
                f"{actor!r}; a rerun cannot tell which source rows were copied"
            )
        for entry in entries:
            source_id = _source_id_from_content(entry.content)
            if source_id is not None:
                found.add((actor, source_id))
    return found


def _marked_content(content: str, source_id: str) -> str:
    """Append the Lakebase id so a later pass can recognize this copy.

    Managed memory keeps ``content`` and a one-line description derived from
    it, and drops tags, importance, embeddings, and metadata.
    """
    return f"{content}\n[{_SOURCE_PREFIX}{source_id}]"


def _source_id_from_content(content: str) -> str | None:
    marker = f"\n[{_SOURCE_PREFIX}"
    if marker not in content:
        return None
    token = content.rsplit(marker, 1)[1]
    if not token.endswith("]"):
        return None
    source_id = token[:-1].strip()
    return source_id or None


def _sqlalchemy() -> Any:
    try:
        import sqlalchemy
    except ImportError as exc:
        raise ImportError(
            "State migration requires SQLAlchemy. Install with: pip install 'apx-agent[lakebase]'"
        ) from exc
    return sqlalchemy


def _quoted_ident(name: str) -> str:
    """Quote a table identifier the caller names, rejecting SQL fragments."""
    parts = name.split(".")
    if not parts or any(not part.isidentifier() for part in parts):
        raise ValueError(f"Refusing non-identifier table name {name!r}")
    return ".".join(f'"{part}"' for part in parts)


__all__ = [
    "MemoryImportResult",
    "MemoryTableInventory",
    "MemoryDestination",
    "import_memories",
    "inventory_memory_table",
]
