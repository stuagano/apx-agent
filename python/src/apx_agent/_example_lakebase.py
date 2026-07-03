"""LakebaseExampleStore — Postgres-backed few-shot example store.

Sibling to :class:`LakebaseMemoryStore`. Persists :class:`Example` rows
with pgvector-backed semantic retrieval over the ``input`` field.

Schema (created on first use when ``auto_create=True``)::

    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE IF NOT EXISTS {table} (
        id          TEXT PRIMARY KEY,
        agent_id    TEXT NOT NULL,
        intent      TEXT NOT NULL DEFAULT 'default',
        input       TEXT NOT NULL,
        output      TEXT NOT NULL,
        score       REAL,
        tags        TEXT[] NOT NULL DEFAULT '{}',
        embedding   vector({dim}),
        metadata    JSONB NOT NULL DEFAULT '{}',
        created_at  DOUBLE PRECISION,
        updated_at  DOUBLE PRECISION
    );
    CREATE INDEX IF NOT EXISTS {table}_agent_idx ON {table} (agent_id);
    CREATE INDEX IF NOT EXISTS {table}_embedding_idx ON {table}
        USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

Requires the ``lakebase`` extra::

    pip install 'apx-agent[lakebase]'
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ._example import (
    Example,
    ExampleFilter,
    ExampleResult,
    FindSimilarOptions,
    apply_example_patch,
    materialize_example,
)
from ._memory import EmbeddingFn
from ._memory_lakebase import (
    _SQLALCHEMY_MISSING,
    _epoch_to_iso,
    _iso_to_epoch,
    _require_sqlalchemy,
    _vector_literal,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# Re-exported for symmetry with the memory module's friendly error message.
_ = _SQLALCHEMY_MISSING


# ---------------------------------------------------------------------------
# Row → Example
# ---------------------------------------------------------------------------


def _row_to_example(row: Mapping[str, Any]) -> Example:
    """Convert a SQL row to an :class:`Example`.

    Handles all the DB-type → Python-type quirks: pgvector strings, JSONB
    coming back as a string from psycopg, TEXT[] arrays, epoch timestamps.
    """
    tags_raw = row.get("tags")
    if tags_raw is None:
        tags: tuple[str, ...] = ()
    elif isinstance(tags_raw, (list, tuple)):
        tags = tuple(str(t) for t in tags_raw)
    else:
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
        stripped = embedding_raw.strip().strip("[]")
        embedding = (
            ()
            if not stripped
            else tuple(float(v) for v in stripped.split(","))
        )
    elif isinstance(embedding_raw, (list, tuple)):
        embedding = tuple(float(v) for v in embedding_raw)
    else:
        embedding = None

    score_raw = row.get("score")
    return Example(
        id=str(row["id"]),
        agent_id=str(row["agent_id"]),
        intent=str(row.get("intent") or "default"),
        input=str(row.get("input") or ""),
        output=str(row.get("output") or ""),
        score=float(score_raw) if score_raw is not None else None,
        tags=tags,
        embedding=embedding,
        metadata=metadata,
        created_at=_epoch_to_iso(row.get("created_at")),
        updated_at=_epoch_to_iso(row.get("updated_at")),
    )


# ---------------------------------------------------------------------------
# LakebaseExampleStore
# ---------------------------------------------------------------------------


class LakebaseExampleStore:
    """:class:`ExampleStore` backed by a Lakebase (or any Postgres) instance.

    Uses pgvector for semantic retrieval — the ``input`` field's embedding
    is compared against the query embedding via ``embedding <=> :q::vector``
    (cosine distance); ``score = 1 - distance``.

    Args:
        engine: SQLAlchemy ``Engine`` connected to the Lakebase instance.
        embedding_fn: Required batched embedding function. Called on
            :meth:`add` (per ``input``), :meth:`add_batch`, and
            :meth:`find_similar` (per query). Constructor raises
            ``ValueError`` if omitted.
        embedding_dim: Required dimensionality of ``embedding_fn``'s output.
        table_name: Defaults to ``"apx_examples"``.
        auto_create: When ``True`` (default), runs the DDL block on first use.
        ensure_extension: When ``True`` (default), the DDL block also runs
            ``CREATE EXTENSION IF NOT EXISTS vector``.
    """

    def __init__(
        self,
        *,
        engine: "Engine",
        embedding_fn: EmbeddingFn,
        embedding_dim: int,
        table_name: str = "apx_examples",
        auto_create: bool = True,
        ensure_extension: bool = True,
    ) -> None:
        """Construct the store. Raises ``ValueError`` on missing required args."""
        if embedding_fn is None:
            raise ValueError("LakebaseExampleStore requires an `embedding_fn`.")
        if embedding_dim is None or int(embedding_dim) <= 0:
            raise ValueError(
                "LakebaseExampleStore requires a positive `embedding_dim`."
            )
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
        """Run the table + index DDL exactly once per instance."""
        if self._created or not self._auto_create:
            return
        self._created = True
        sa = _require_sqlalchemy()
        table = self.table_name
        statements: list[str] = []
        if self._ensure_extension:
            statements.append("CREATE EXTENSION IF NOT EXISTS vector")
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            f"  id TEXT PRIMARY KEY,"
            f"  agent_id TEXT NOT NULL,"
            f"  intent TEXT NOT NULL DEFAULT 'default',"
            f"  input TEXT NOT NULL,"
            f"  output TEXT NOT NULL,"
            f"  score REAL,"
            f"  tags TEXT[] NOT NULL DEFAULT '{{}}',"
            f"  embedding vector({self.embedding_dim}),"
            f"  metadata JSONB NOT NULL DEFAULT '{{}}',"
            f"  created_at DOUBLE PRECISION,"
            f"  updated_at DOUBLE PRECISION"
            f")"
        )
        statements.append(
            f"CREATE INDEX IF NOT EXISTS {table}_agent_idx "
            f"ON {table} (agent_id)"
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
        except Exception as e:
            logger.warning(
                "LakebaseExampleStore: DDL on %s failed: %s — "
                "subsequent reads/writes may fail until the table exists.",
                table, e,
            )

    # -- ExampleStore protocol ---------------------------------------------

    def add(self, example: Mapping[str, Any]) -> Example:
        """Insert one example; returns the materialized row."""
        self._ensure_schema()
        embedding = self._embedding_fn([str(example["input"])])[0]
        materialized = materialize_example(example, embedding=embedding)
        self._insert_rows([materialized])
        return materialized

    def add_batch(self, examples: Sequence[Mapping[str, Any]]) -> list[Example]:
        """Insert many examples in one round-trip."""
        if len(examples) == 0:
            return []
        self._ensure_schema()
        embeddings = self._embedding_fn([str(e["input"]) for e in examples])
        out = [
            materialize_example(raw, embedding=emb)
            for raw, emb in zip(examples, embeddings)
        ]
        self._insert_rows(out)
        return out

    def _insert_rows(self, rows: Sequence[Example]) -> None:
        """Execute the per-row UPSERT SQL for a batch of materialized rows."""
        sa = _require_sqlalchemy()
        sql = sa.text(
            f"INSERT INTO {self.table_name} "
            f"(id, agent_id, intent, input, output, score, tags, "
            f" embedding, metadata, created_at, updated_at) "
            f"VALUES (:id, :agent_id, :intent, :input, :output, :score, :tags, "
            f"        CAST(:embedding AS vector), CAST(:metadata AS jsonb), "
            f"        :created_at, :updated_at) "
            f"ON CONFLICT (id) DO UPDATE SET "
            f"  agent_id = EXCLUDED.agent_id, "
            f"  intent = EXCLUDED.intent, "
            f"  input = EXCLUDED.input, "
            f"  output = EXCLUDED.output, "
            f"  score = EXCLUDED.score, "
            f"  tags = EXCLUDED.tags, "
            f"  embedding = EXCLUDED.embedding, "
            f"  metadata = EXCLUDED.metadata, "
            f"  updated_at = EXCLUDED.updated_at"
        )
        with self.engine.begin() as conn:
            for ex in rows:
                conn.execute(sql, self._row_params(ex))

    def _row_params(self, ex: Example) -> dict[str, Any]:
        """Build the bind params for an insert/update of one :class:`Example`."""
        return {
            "id": ex.id,
            "agent_id": ex.agent_id,
            "intent": ex.intent,
            "input": ex.input,
            "output": ex.output,
            "score": ex.score,
            "tags": list(ex.tags),
            "embedding": _vector_literal(ex.embedding),
            "metadata": json.dumps(dict(ex.metadata), default=str),
            "created_at": _iso_to_epoch(ex.created_at),
            "updated_at": _iso_to_epoch(ex.updated_at),
        }

    def get(self, example_id: str) -> Example | None:
        """Return the example with ``example_id``, or ``None`` if missing."""
        self._ensure_schema()
        sa = _require_sqlalchemy()
        sql = sa.text(
            f"SELECT id, agent_id, intent, input, output, score, tags, "
            f" embedding, metadata, created_at, updated_at "
            f"FROM {self.table_name} WHERE id = :id LIMIT 1"
        )
        try:
            with self.engine.connect() as conn:
                row = conn.execute(sql, {"id": example_id}).mappings().first()
        except Exception as e:
            logger.warning(
                "LakebaseExampleStore.get(%s) failed: %s — returning None.",
                example_id, e,
            )
            return None
        if row is None:
            return None
        return _row_to_example(dict(row))

    def update(self, example_id: str, patch: Mapping[str, Any]) -> Example | None:
        """Apply ``patch`` and return the updated row, or ``None`` if missing."""
        existing = self.get(example_id)
        if existing is None:
            return None
        updated = apply_example_patch(existing, patch)
        if "input" in patch:
            new_emb = self._embedding_fn([str(patch["input"])])[0]
            updated = Example(
                id=updated.id,
                agent_id=updated.agent_id,
                intent=updated.intent,
                input=updated.input,
                output=updated.output,
                score=updated.score,
                tags=updated.tags,
                embedding=tuple(float(v) for v in new_emb),
                metadata=updated.metadata,
                created_at=updated.created_at,
                updated_at=updated.updated_at,
            )
        self._insert_rows([updated])
        return updated

    def delete(self, example_id: str) -> bool:
        """Delete the row; returns ``True`` on hit, ``False`` on miss."""
        self._ensure_schema()
        sa = _require_sqlalchemy()
        sql = sa.text(f"DELETE FROM {self.table_name} WHERE id = :id")
        try:
            with self.engine.begin() as conn:
                result = conn.execute(sql, {"id": example_id})
        except Exception as e:
            logger.warning(
                "LakebaseExampleStore.delete(%s) failed: %s", example_id, e
            )
            return False
        rowcount = getattr(result, "rowcount", -1)
        return rowcount is None or rowcount > 0

    def list(self, filter: ExampleFilter) -> list[Example]:
        """Return rows matching ``filter``, sorted by ``updated_at`` desc."""
        self._ensure_schema()
        sa = _require_sqlalchemy()
        where_clauses = ["agent_id = :agent_id"]
        params: dict[str, Any] = {
            "agent_id": filter.agent_id,
            "limit": filter.limit,
        }
        if filter.intent is not None:
            where_clauses.append("intent = :intent")
            params["intent"] = filter.intent
        if filter.tags:
            where_clauses.append("tags && CAST(:tags AS TEXT[])")
            params["tags"] = list(filter.tags)
        if filter.min_score is not None:
            # Rows with NULL score are excluded — mirrors InMemory behavior.
            where_clauses.append("score IS NOT NULL AND score >= :min_score")
            params["min_score"] = float(filter.min_score)
        sql = sa.text(
            f"SELECT id, agent_id, intent, input, output, score, tags, "
            f" embedding, metadata, created_at, updated_at "
            f"FROM {self.table_name} "
            f"WHERE {' AND '.join(where_clauses)} "
            f"ORDER BY updated_at DESC "
            f"LIMIT :limit"
        )
        # Let infra failures propagate: a swallowed error here would read as "no
        # examples" to prompt assembly and silently degrade answer quality.
        # Reserve [] for a genuinely empty result set. (matches the memory store, H21) (#380)
        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
        return [_row_to_example(dict(r)) for r in rows]

    def find_similar(self, opts: FindSimilarOptions) -> list[ExampleResult]:
        """pgvector retrieval: ``embedding <=> :q::vector`` = cosine distance.

        ``score = 1 - distance``; ordered ascending by distance.
        """
        self._ensure_schema()
        sa = _require_sqlalchemy()
        query_emb = self._embedding_fn([opts.query])[0]
        where_clauses = ["agent_id = :agent_id"]
        params: dict[str, Any] = {
            "agent_id": opts.agent_id,
            "query_vec": _vector_literal(query_emb),
            "k": opts.k,
        }
        if opts.intent is not None:
            where_clauses.append("intent = :intent")
            params["intent"] = opts.intent
        if opts.tags:
            where_clauses.append("tags && CAST(:tags AS TEXT[])")
            params["tags"] = list(opts.tags)
        if opts.min_score is not None:
            where_clauses.append("score IS NOT NULL AND score >= :min_score")
            params["min_score"] = float(opts.min_score)
        where_clauses.append("embedding IS NOT NULL")
        sql = sa.text(
            f"SELECT id, agent_id, intent, input, output, score, tags, "
            f" embedding, metadata, created_at, updated_at, "
            f" 1 - (embedding <=> CAST(:query_vec AS vector)) AS sim "
            f"FROM {self.table_name} "
            f"WHERE {' AND '.join(where_clauses)} "
            f"ORDER BY embedding <=> CAST(:query_vec AS vector) ASC "
            f"LIMIT :k"
        )
        # Let infra failures propagate (see .list) — a swallowed error would read
        # as "no examples" and silently degrade answer quality. (#380)
        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
        results: list[ExampleResult] = []
        for r in rows:
            d = dict(r)
            sim = float(d.pop("sim", 0.0) or 0.0)
            results.append(ExampleResult(example=_row_to_example(d), score=sim))
        return results


__all__ = ["LakebaseExampleStore"]
