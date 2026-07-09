"""LakebaseMemoryStore — Postgres-backed durable memory over Databricks Lakebase.

Lakebase is Databricks' managed Postgres, offering:

  * Low per-operation latency (Postgres point lookups).
  * pgvector semantic recall in SQL (no client-side ranking).
  * Good fit for high-frequency interactive recall.

Mirrors the design of :mod:`apx_agent._session_lakebase` — same SQLAlchemy
``Engine``-in-constructor pattern, same lazy DDL via ``_ensure_schema``, same
raw ``text()`` SQL. The store stays narrow: SQL only. Token rotation,
connection pooling, and OAuth wiring are the caller's responsibility.

Schema (created on first use when ``auto_create=True``)::

    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE IF NOT EXISTS {table} (
        id           TEXT PRIMARY KEY,
        principal_id TEXT NOT NULL,
        namespace    TEXT NOT NULL DEFAULT 'default',
        content      TEXT NOT NULL,
        tags         TEXT[] NOT NULL DEFAULT '{}',
        importance   REAL NOT NULL DEFAULT 0.5,
        embedding    vector({dim}),
        metadata     JSONB NOT NULL DEFAULT '{}',
        created_at   DOUBLE PRECISION,
        updated_at   DOUBLE PRECISION
    );
    CREATE INDEX IF NOT EXISTS {table}_principal_idx ON {table} (principal_id);
    CREATE INDEX IF NOT EXISTS {table}_embedding_idx ON {table}
        USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

Requires the ``lakebase`` extra::

    pip install 'apx-agent[lakebase]'
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ._memory import (
    EmbeddingFn,
    Memory,
    MemoryFilter,
    RecallOptions,
    RecallResult,
    apply_patch,
    iso_now,
    materialize_memory,
)
from ._memory import iso_to_epoch as _iso_to_epoch
from ._memory import validate_table_name as _validate_table_name

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


_SQLALCHEMY_MISSING = (
    "LakebaseMemoryStore requires SQLAlchemy. "
    "Install with: pip install 'apx-agent[lakebase]'"
)


def _require_sqlalchemy() -> Any:
    try:
        import sqlalchemy
    except ImportError as e:  # pragma: no cover — exercised only without extra
        raise ImportError(_SQLALCHEMY_MISSING) from e
    return sqlalchemy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _epoch_to_iso(seconds: float | None) -> str:
    """Convert UNIX epoch seconds to ISO-8601. Returns now-ISO on garbage."""
    if seconds is None or seconds <= 0:
        return iso_now()
    dt = datetime.fromtimestamp(float(seconds), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _vector_literal(embedding: Sequence[float] | None) -> str | None:
    """Serialize an embedding as a pgvector textual literal: ``'[v1,v2,...]'``.

    Returns ``None`` when the embedding itself is ``None``. The caller binds
    this through a parameter and casts to ``::vector`` in SQL.
    """
    if embedding is None:
        return None
    return "[" + ",".join(repr(float(v)) for v in embedding) + "]"


def _row_to_memory(row: Mapping[str, Any]) -> Memory:
    """Convert a SQL row to a :class:`Memory`.

    Handles all the DB-type → Python-type quirks: pgvector strings, JSONB
    coming back as a string from psycopg, TEXT[] arrays, epoch timestamps.
    """
    tags_raw = row.get("tags")
    if tags_raw is None:
        tags: tuple[str, ...] = ()
    elif isinstance(tags_raw, (list, tuple)):
        tags = tuple(str(t) for t in tags_raw)
    else:
        # Unusual driver shape — best-effort
        tags = (str(tags_raw),) if tags_raw else ()

    metadata_raw = row.get("metadata")
    if metadata_raw is None:
        metadata: dict[str, Any] = {}
    elif isinstance(metadata_raw, str):
        try:
            metadata = json.loads(metadata_raw)
        except json.JSONDecodeError:
            metadata = {}
    elif isinstance(metadata_raw, dict):
        metadata = dict(metadata_raw)
    else:
        metadata = {}

    embedding_raw = row.get("embedding")
    embedding: tuple[float, ...] | None
    if embedding_raw is None:
        embedding = None
    elif isinstance(embedding_raw, str):
        # pgvector returns '[v1,v2,...]'
        stripped = embedding_raw.strip().strip("[]")
        if not stripped:
            embedding = ()
        else:
            embedding = tuple(float(v) for v in stripped.split(","))
    elif isinstance(embedding_raw, (list, tuple)):
        embedding = tuple(float(v) for v in embedding_raw)
    else:
        embedding = None

    return Memory(
        id=str(row["id"]),
        principal_id=str(row["principal_id"]),
        namespace=str(row.get("namespace") or "default"),
        content=str(row.get("content") or ""),
        tags=tags,
        importance=float(row.get("importance") or 0.0),
        embedding=embedding,
        metadata=metadata,
        created_at=_epoch_to_iso(row.get("created_at")),
        updated_at=_epoch_to_iso(row.get("updated_at")),
    )


# ---------------------------------------------------------------------------
# LakebaseMemoryStore
# ---------------------------------------------------------------------------


class LakebaseMemoryStore:
    """:class:`MemoryStore` backed by a Lakebase (or any Postgres) instance.

    Uses pgvector for semantic recall — ``embedding <=> :q::vector`` returns
    cosine distance, and ``score = 1 - distance`` gives a 0..1 similarity.

    Args:
        engine: SQLAlchemy ``Engine`` connected to the Lakebase instance. The
            caller is responsible for token rotation — typically a
            ``do_connect`` event listener that mints fresh OAuth tokens.
        embedding_fn: Required batched embedding function. Called on
            :meth:`add` (per content), :meth:`add_batch` (in one call), and
            :meth:`recall` (per query). Constructor raises ``ValueError`` if
            omitted.
        embedding_dim: Required dimensionality of ``embedding_fn``'s output.
            Wired into the ``vector(N)`` column type. Must match the function.
        table_name: Table name for rows. Defaults to ``"apx_memories"``.
            Validated against an allowlist of safe identifier characters
            (alnum, underscore, dot), so it can include a schema prefix.
        auto_create: When ``True`` (default), runs the DDL block on first use.
        ensure_extension: When ``True`` (default), the DDL block also runs
            ``CREATE EXTENSION IF NOT EXISTS vector``. Set ``False`` when the
            caller's role lacks the privilege.

    Example::

        from sqlalchemy import create_engine, event
        from databricks.sdk import WorkspaceClient
        from apx_agent import LakebaseMemoryStore

        ws = WorkspaceClient()
        engine = create_engine("postgresql+psycopg://user@host/agentdb")

        @event.listens_for(engine, "do_connect")
        def add_oauth_token(_dialect, _record, _args, kwargs):
            cred = ws.database.generate_database_credential(
                instance_names=["my-lakebase-instance"], request_id="apx-memory",
            )
            kwargs["password"] = cred.token

        store = LakebaseMemoryStore(
            engine=engine, embedding_fn=my_embedder, embedding_dim=1024,
        )
    """

    def __init__(
        self,
        *,
        engine: "Engine",
        embedding_fn: EmbeddingFn,
        embedding_dim: int,
        table_name: str = "apx_memories",
        auto_create: bool = True,
        ensure_extension: bool = True,
    ) -> None:
        """Construct the store. Raises ``ValueError`` on missing required args."""
        if embedding_fn is None:
            raise ValueError("LakebaseMemoryStore requires an `embedding_fn`.")
        if embedding_dim is None or int(embedding_dim) <= 0:
            raise ValueError(
                "LakebaseMemoryStore requires a positive `embedding_dim`."
            )
        _validate_table_name(table_name)
        _require_sqlalchemy()
        self.engine = engine
        self._embedding_fn = embedding_fn
        self.embedding_dim = int(embedding_dim)
        self.table_name = table_name
        self._auto_create = auto_create
        self._ensure_extension = ensure_extension
        self._created = False

    # -- DDL ----------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Run the table + index DDL exactly once per instance.

        Latch ``_created`` only after the DDL succeeds, so a transient or
        permission failure self-heals on the next op instead of permanently
        no-opping schema creation (get/delete would silently return None/False
        and add/list/recall would raise undefined-table).
        """
        if self._created or not self._auto_create:
            return
        sa = _require_sqlalchemy()
        table = self.table_name
        statements: list[str] = []
        if self._ensure_extension:
            statements.append("CREATE EXTENSION IF NOT EXISTS vector")
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            f"  id TEXT PRIMARY KEY,"
            f"  principal_id TEXT NOT NULL,"
            f"  namespace TEXT NOT NULL DEFAULT 'default',"
            f"  content TEXT NOT NULL,"
            f"  tags TEXT[] NOT NULL DEFAULT '{{}}',"
            f"  importance REAL NOT NULL DEFAULT 0.5,"
            f"  embedding vector({self.embedding_dim}),"
            f"  metadata JSONB NOT NULL DEFAULT '{{}}',"
            f"  created_at DOUBLE PRECISION,"
            f"  updated_at DOUBLE PRECISION"
            f")"
        )
        statements.append(
            f"CREATE INDEX IF NOT EXISTS {table}_principal_idx "
            f"ON {table} (principal_id)"
        )
        statements.append(
            f"CREATE INDEX IF NOT EXISTS {table}_embedding_idx "
            f"ON {table} USING ivfflat (embedding vector_cosine_ops) "
            f"WITH (lists = 100)"
        )
        try:
            with self.engine.begin() as conn:
                for stmt in statements:
                    conn.execute(sa.text(stmt))
            self._created = True
        except Exception as e:
            logger.warning(
                "LakebaseMemoryStore: DDL on %s failed: %s — "
                "subsequent reads/writes may fail until the table exists.",
                table, e,
            )

    # -- MemoryStore protocol ----------------------------------------------

    def add(self, memory: Mapping[str, Any]) -> Memory:
        """Insert one memory; returns the materialized row."""
        self._ensure_schema()
        embedding = self._embedding_fn([str(memory["content"])])[0]
        materialized = materialize_memory(memory, embedding=embedding)
        self._insert_rows([materialized])
        return materialized

    def add_batch(self, memories: Sequence[Mapping[str, Any]]) -> list[Memory]:
        """Insert many memories in one round-trip."""
        if len(memories) == 0:
            return []
        self._ensure_schema()
        embeddings = self._embedding_fn([str(m["content"]) for m in memories])
        out = [
            materialize_memory(raw, embedding=emb)
            for raw, emb in zip(memories, embeddings)
        ]
        self._insert_rows(out)
        return out

    def _insert_rows(self, rows: Sequence[Memory]) -> None:
        """Execute the per-row UPSERT SQL for a batch of materialized rows."""
        sa = _require_sqlalchemy()
        sql = sa.text(
            f"INSERT INTO {self.table_name} "
            f"(id, principal_id, namespace, content, tags, importance, "
            f" embedding, metadata, created_at, updated_at) "
            f"VALUES (:id, :principal_id, :namespace, :content, :tags, "
            f"        :importance, CAST(:embedding AS vector), "
            f"        CAST(:metadata AS jsonb), :created_at, :updated_at) "
            f"ON CONFLICT (id) DO UPDATE SET "
            f"  principal_id = EXCLUDED.principal_id, "
            f"  namespace = EXCLUDED.namespace, "
            f"  content = EXCLUDED.content, "
            f"  tags = EXCLUDED.tags, "
            f"  importance = EXCLUDED.importance, "
            f"  embedding = EXCLUDED.embedding, "
            f"  metadata = EXCLUDED.metadata, "
            f"  updated_at = EXCLUDED.updated_at"
        )
        with self.engine.begin() as conn:
            for m in rows:
                conn.execute(sql, self._row_params(m))

    def _row_params(self, m: Memory) -> dict[str, Any]:
        """Build the bind params for an insert/update of one :class:`Memory`."""
        return {
            "id": m.id,
            "principal_id": m.principal_id,
            "namespace": m.namespace,
            "content": m.content,
            # SQLAlchemy + psycopg handles list[str] -> TEXT[] directly.
            "tags": list(m.tags),
            "importance": m.importance,
            "embedding": _vector_literal(m.embedding),
            "metadata": json.dumps(dict(m.metadata), default=str),
            "created_at": _iso_to_epoch(m.created_at),
            "updated_at": _iso_to_epoch(m.updated_at),
        }

    def get(self, memory_id: str) -> Memory | None:
        """Return the memory with ``memory_id``, or ``None`` if genuinely missing.

        Infra errors propagate — a transient failure must not masquerade as
        "not found" (which callers like update/delete/forget read as a genuine
        miss). ``None`` is reserved for a real empty result. (H21: same posture
        as ``list``/``recall``.)
        """
        self._ensure_schema()
        sa = _require_sqlalchemy()
        sql = sa.text(
            f"SELECT id, principal_id, namespace, content, tags, importance, "
            f" embedding, metadata, created_at, updated_at "
            f"FROM {self.table_name} WHERE id = :id LIMIT 1"
        )
        with self.engine.connect() as conn:
            row = conn.execute(sql, {"id": memory_id}).mappings().first()
        if row is None:
            return None
        return _row_to_memory(dict(row))

    def update(self, memory_id: str, patch: Mapping[str, Any]) -> Memory | None:
        """Apply ``patch`` and return the updated row, or ``None`` if missing."""
        existing = self.get(memory_id)
        if existing is None:
            return None
        updated = apply_patch(existing, patch)
        if "content" in patch:
            new_emb = self._embedding_fn([str(patch["content"])])[0]
            updated = Memory(
                id=updated.id,
                principal_id=updated.principal_id,
                namespace=updated.namespace,
                content=updated.content,
                tags=updated.tags,
                importance=updated.importance,
                embedding=tuple(float(v) for v in new_emb),
                metadata=updated.metadata,
                created_at=updated.created_at,
                updated_at=updated.updated_at,
            )
        if not self._update_row(updated):
            # The row was deleted between the get() above and this UPDATE — a
            # conditional UPDATE affects 0 rows rather than re-inserting it, so
            # a concurrent forget() is not silently undone (#485).
            return None
        return updated

    def _update_row(self, m: Memory) -> bool:
        """UPDATE an EXISTING row by id; ``True`` when a row was updated.

        Unlike :meth:`_insert_rows` (an upsert used by ``add``), this never
        re-creates a row, so an update racing a concurrent delete touches 0 rows
        and reports ``False`` instead of resurrecting the deleted memory.
        """
        sa = _require_sqlalchemy()
        sql = sa.text(
            f"UPDATE {self.table_name} SET "
            f"  principal_id = :principal_id, namespace = :namespace, "
            f"  content = :content, tags = :tags, importance = :importance, "
            f"  embedding = CAST(:embedding AS vector), "
            f"  metadata = CAST(:metadata AS jsonb), updated_at = :updated_at "
            f"WHERE id = :id"
        )
        with self.engine.begin() as conn:
            result = conn.execute(sql, self._row_params(m))
        rowcount = getattr(result, "rowcount", -1)
        return rowcount is None or rowcount > 0

    def delete(self, memory_id: str) -> bool:
        """Delete the row; returns ``True`` on hit, ``False`` on miss.

        Infra errors propagate — ``False`` means the row wasn't there, never
        "the delete failed" (which a caller would misread as "already gone").
        """
        self._ensure_schema()
        sa = _require_sqlalchemy()
        sql = sa.text(
            f"DELETE FROM {self.table_name} WHERE id = :id"
        )
        with self.engine.begin() as conn:
            result = conn.execute(sql, {"id": memory_id})
        rowcount = getattr(result, "rowcount", -1)
        return rowcount is None or rowcount > 0

    def list(self, filter: MemoryFilter) -> list[Memory]:
        """Return rows matching ``filter``, sorted by ``updated_at`` desc."""
        self._ensure_schema()
        sa = _require_sqlalchemy()
        where_clauses = ["principal_id = :principal_id"]
        params: dict[str, Any] = {
            "principal_id": filter.principal_id,
            "limit": filter.limit,
        }
        if filter.namespace is not None:
            where_clauses.append("namespace = :namespace")
            params["namespace"] = filter.namespace
        if filter.tags:
            where_clauses.append("tags && CAST(:tags AS TEXT[])")
            params["tags"] = list(filter.tags)
        if filter.min_importance is not None:
            where_clauses.append("importance >= :min_importance")
            params["min_importance"] = float(filter.min_importance)
        sql = sa.text(
            f"SELECT id, principal_id, namespace, content, tags, importance, "
            f" embedding, metadata, created_at, updated_at "
            f"FROM {self.table_name} "
            f"WHERE {' AND '.join(where_clauses)} "
            f"ORDER BY updated_at DESC "
            f"LIMIT :limit"
        )
        # Let infra failures propagate: a swallowed error here would read
        # as "no memories found" to the recall tool. Reserve [] (an empty
        # result set) for a genuinely empty table. (H21)
        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
        return [_row_to_memory(dict(r)) for r in rows]

    def recall(self, opts: RecallOptions) -> list[RecallResult]:
        """pgvector recall: ``embedding <=> :q::vector`` returns cosine distance.

        ``score = 1 - distance``; results are ordered ascending by distance
        (i.e. most-similar first), then capped at ``k``.
        """
        self._ensure_schema()
        sa = _require_sqlalchemy()
        query_emb = self._embedding_fn([opts.query])[0]
        # Validate dimensionality up front (mirrors the TS store): a
        # mismatch must raise here rather than surface as a swallowed DB
        # error → []. (M30)
        if query_emb is None or len(query_emb) != self.embedding_dim:
            raise ValueError(
                f"LakebaseMemoryStore.recall: embedder returned "
                f"{0 if query_emb is None else len(query_emb)} dims, "
                f"expected {self.embedding_dim}."
            )
        where_clauses = ["principal_id = :principal_id"]
        params: dict[str, Any] = {
            "principal_id": opts.principal_id,
            "query_vec": _vector_literal(query_emb),
            "k": opts.k,
        }
        if opts.namespace is not None:
            where_clauses.append("namespace = :namespace")
            params["namespace"] = opts.namespace
        if opts.tags:
            where_clauses.append("tags && CAST(:tags AS TEXT[])")
            params["tags"] = list(opts.tags)
        if opts.min_importance is not None:
            where_clauses.append("importance >= :min_importance")
            params["min_importance"] = float(opts.min_importance)
        # Skip rows without embeddings — they cannot be ranked.
        where_clauses.append("embedding IS NOT NULL")
        sql = sa.text(
            f"SELECT id, principal_id, namespace, content, tags, importance, "
            f" embedding, metadata, created_at, updated_at, "
            f" 1 - (embedding <=> CAST(:query_vec AS vector)) AS score "
            f"FROM {self.table_name} "
            f"WHERE {' AND '.join(where_clauses)} "
            f"ORDER BY embedding <=> CAST(:query_vec AS vector) ASC "
            f"LIMIT :k"
        )
        # Let infra failures propagate: a swallowed error here would read
        # as "no memories found" to the recall tool. Reserve [] (an empty
        # result set) for a genuinely empty match. (H21)
        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
        results: list[RecallResult] = []
        for r in rows:
            d = dict(r)
            score = float(d.pop("score", 0.0) or 0.0)
            results.append(RecallResult(memory=_row_to_memory(d), score=score))
        return results


__all__ = ["LakebaseMemoryStore"]
