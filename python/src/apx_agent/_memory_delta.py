"""DeltaMemoryStore — Unity Catalog Delta-backed :class:`MemoryStore`.

Sibling to :mod:`apx_agent._session_delta`. Persists per-principal memories
to a Delta table; supports semantic recall via three pluggable paths:

  1. **Vector Search + local embedder** — caller-side embed the query and
     delegate the cosine search to a Databricks Vector Search index.
     Avoids embedder drift: writes use the same ``embedding_fn`` the index
     was built with.
  2. **Vector Search only** — pass ``query_text`` to the index and let the
     backend embed.
  3. **Embedder only** — list candidates via SQL (with WHERE-side filters
     for principal / namespace / tags / importance), compute cosine
     client-side, return top-k.
  4. **Neither** — recency fallback: ``ORDER BY updated_at DESC LIMIT k``.

Same injectable-dependency philosophy as the rest of the framework: the
caller passes ``run_sql`` (a ``Callable[[str], list[dict]]``) and an
optional :class:`VectorSearchLike` client. No Databricks SDK imports here.

Mirrors ``typescript/src/memory-delta.ts``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from ._memory import (
    EmbeddingFn,
    Memory,
    MemoryFilter,
    MemoryStore,
    RecallOptions,
    RecallResult,
    apply_patch,
    cosine_similarity,
    iso_now,
    materialize_memory,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vector Search delegate
# ---------------------------------------------------------------------------


@runtime_checkable
class VectorSearchLike(Protocol):
    """Duck-typed Vector Search client.

    When provided, :meth:`DeltaMemoryStore.recall` delegates similarity
    search to the backend index. The shape matches Databricks Vector Search
    SDK conventions but is intentionally a Protocol — any backend can be
    wired in (mocks, alternative vector databases, etc.).

    Implementations return a dict with a ``"rows"`` key whose value is a
    list of row dicts. Each row should include the columns requested in
    ``columns`` plus a similarity score under ``"_score"`` or ``"score"``.
    """

    def similarity_search(
        self,
        *,
        index_name: str,
        query_vector: list[float] | None = None,
        query_text: str | None = None,
        columns: list[str],
        num_results: int,
        filters: dict | None = None,
    ) -> dict:
        """Run a similarity search; returns ``{"rows": [{col: val, ...}, ...]}``."""
        ...


# ---------------------------------------------------------------------------
# SQL escaping helpers
# ---------------------------------------------------------------------------


def _quote_string(value: str) -> str:
    """SQL-escape a string literal: backslashes then single quotes doubled.

    Spark SQL treats backslash as an escape character inside string
    literals, so backslashes must be doubled *before* single quotes are
    doubled — otherwise a trailing ``\\`` would escape the closing quote
    (unterminated literal) and an embedded ``\\'`` would corrupt the
    literal / open an injection vector.

    All untrusted text must pass through this before being embedded in a
    SQL statement. Identifier names (table_name) are validated separately
    by the constructor.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _sql_array_str(items: Sequence[str] | None) -> str:
    """Render an ARRAY<STRING> Spark SQL literal.

    Empty or ``None`` collapses to ``array()`` (typed empty array).
    """
    if items is None or len(items) == 0:
        return "array()"
    return "array(" + ", ".join(_quote_string(s) for s in items) + ")"


def _sql_array_float(items: Sequence[float] | None) -> str:
    """Render an ARRAY<FLOAT> Spark SQL literal.

    ``None`` emits a typed NULL so the column shape stays ``ARRAY<FLOAT>``.
    Non-finite floats are coerced to 0 to avoid SQL ``NaN``/``Infinity``
    literals that some Spark versions reject.
    """
    if items is None:
        return "CAST(NULL AS ARRAY<FLOAT>)"
    if len(items) == 0:
        return "array()"

    def _fmt(n: float) -> str:
        try:
            f = float(n)
        except (TypeError, ValueError):
            return "0"
        # Reject NaN / inf -> 0
        if f != f or f == float("inf") or f == float("-inf"):
            return "0"
        return repr(f)

    return "array(" + ", ".join(_fmt(n) for n in items) + ")"


def _iso_to_epoch(iso: str | None) -> float:
    """Convert ISO-8601 to UNIX epoch seconds. Returns 0 on garbage."""
    if not iso:
        return 0.0
    try:
        s = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


def _epoch_to_iso(seconds: float | None) -> str:
    """Convert UNIX epoch seconds to ISO-8601 (UTC). Returns now on garbage."""
    if seconds is None or seconds <= 0:
        return iso_now()
    try:
        dt = datetime.fromtimestamp(float(seconds), tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    except (TypeError, ValueError, OSError):
        return iso_now()


def _row_to_memory(row: Mapping[str, Any]) -> Memory:
    """Convert a raw driver row dict to a :class:`Memory` instance.

    Tolerates string-encoded JSON metadata, list-typed tags/embedding, and
    missing optional columns.
    """
    # metadata can come back as a JSON string (Delta STRING) or a dict
    metadata_raw = row.get("metadata")
    if isinstance(metadata_raw, str) and metadata_raw:
        try:
            metadata: Mapping[str, Any] = json.loads(metadata_raw)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    elif isinstance(metadata_raw, Mapping):
        metadata = dict(metadata_raw)
    else:
        metadata = {}

    tags_raw = row.get("tags")
    if isinstance(tags_raw, (list, tuple)):
        tags = tuple(str(t) for t in tags_raw)
    else:
        tags = ()

    emb_raw = row.get("embedding")
    if isinstance(emb_raw, (list, tuple)) and len(emb_raw) > 0:
        embedding: tuple[float, ...] | None = tuple(float(v) for v in emb_raw)
    else:
        embedding = None

    return Memory(
        id=str(row.get("id") or ""),
        principal_id=str(row.get("principal_id") or ""),
        namespace=str(row.get("namespace") or "default"),
        content=str(row.get("content") or ""),
        tags=tags,
        importance=float(row.get("importance") if row.get("importance") is not None else 0.5),
        embedding=embedding,
        metadata=metadata,
        created_at=_epoch_to_iso(row.get("created_at")),
        updated_at=_epoch_to_iso(row.get("updated_at")),
    )


# ---------------------------------------------------------------------------
# Table-name validation
# ---------------------------------------------------------------------------


_TABLE_NAME_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_."
)


def _validate_table_name(name: str) -> None:
    """Reject table names that can't be a UC identifier.

    Allows alnum, underscore, and dot (for ``catalog.schema.table``).
    Anything else risks SQL injection through identifier interpolation.
    """
    if not name:
        raise ValueError("table_name must not be empty")
    for ch in name:
        if ch not in _TABLE_NAME_ALLOWED:
            raise ValueError(
                f"table_name contains illegal character {ch!r}: {name!r}"
            )


# ---------------------------------------------------------------------------
# DeltaMemoryStore
# ---------------------------------------------------------------------------


class DeltaMemoryStore:
    """Delta-backed :class:`MemoryStore` implementation.

    All SQL execution flows through the injected ``run_sql`` callable so
    the caller controls auth (user-scoped OBO vs. service-principal), the
    SQL warehouse, and the transport (CLI, SDK, REST, fake-for-tests).
    """

    def __init__(
        self,
        *,
        run_sql: Callable[[str], list[dict]],
        embedding_fn: EmbeddingFn | None = None,
        embedding_dim: int | None = None,
        table_name: str = "apx_memories",
        auto_create: bool = True,
        vector_search: VectorSearchLike | None = None,
        index_name: str | None = None,
    ) -> None:
        """Construct a Delta-backed memory store.

        Args:
            run_sql: Callable that executes a SQL string and returns the
                result rows as a list of dicts (one dict per row, keyed by
                column name). For DDL or write statements the returned
                list is typically empty.
            embedding_fn: Optional batched embedding function. When set,
                content is embedded on write and queries are embedded
                client-side for vector-search delegation (path 1).
            embedding_dim: Informational; ARRAY<FLOAT> doesn't constrain
                length, but the caller can record what dimensionality the
                ``embedding_fn`` returns.
            table_name: Fully-qualified UC table name. Validated against
                an allowlist of safe identifier characters.
            auto_create: Issue ``CREATE TABLE IF NOT EXISTS`` on first use.
                Set False when the table is provisioned out-of-band.
            vector_search: Optional :class:`VectorSearchLike` delegate.
                When set together with ``index_name``, ``recall`` routes
                through it instead of doing SQL + client-side cosine.
            index_name: Vector Search index name. Required when
                ``vector_search`` is set.
        """
        _validate_table_name(table_name)
        if vector_search is not None and not index_name:
            raise ValueError(
                "index_name is required when vector_search is provided"
            )
        self._run_sql = run_sql
        self._embedding_fn = embedding_fn
        self.embedding_dim = embedding_dim
        self.table_name = table_name
        self._auto_create = auto_create
        self._vector_search = vector_search
        self._index_name = index_name
        self._created = False

    # -- DDL ----------------------------------------------------------------

    def _ensure_table(self) -> None:
        """Idempotently issue the ``CREATE TABLE IF NOT EXISTS`` once."""
        if self._created or not self._auto_create:
            self._created = True
            return
        sql = (
            f"CREATE TABLE IF NOT EXISTS {self.table_name} ("
            f"  id STRING,"
            f"  principal_id STRING,"
            f"  namespace STRING,"
            f"  content STRING,"
            f"  tags ARRAY<STRING>,"
            f"  importance FLOAT,"
            f"  embedding ARRAY<FLOAT>,"
            f"  metadata STRING,"
            f"  created_at DOUBLE,"
            f"  updated_at DOUBLE"
            f") USING DELTA"
        )
        try:
            self._run_sql(sql)
        except Exception as e:
            logger.warning(
                "DeltaMemoryStore: CREATE TABLE IF NOT EXISTS %s failed: %s — "
                "subsequent reads/writes may fail until the table exists.",
                self.table_name, e,
            )
        self._created = True

    # -- MERGE SQL ----------------------------------------------------------

    def _merge_sql(self, memory: Memory) -> str:
        """Build the ``MERGE INTO`` upsert SQL for one row."""
        created_at = _iso_to_epoch(memory.created_at)
        updated_at = _iso_to_epoch(memory.updated_at)
        metadata_json = json.dumps(dict(memory.metadata or {}), default=str)
        return (
            f"MERGE INTO {self.table_name} AS target "
            f"USING (SELECT "
            f"  {_quote_string(memory.id)} AS id, "
            f"  {_quote_string(memory.principal_id)} AS principal_id, "
            f"  {_quote_string(memory.namespace)} AS namespace, "
            f"  {_quote_string(memory.content)} AS content, "
            f"  {_sql_array_str(memory.tags)} AS tags, "
            f"  CAST({float(memory.importance)} AS FLOAT) AS importance, "
            f"  {_sql_array_float(memory.embedding)} AS embedding, "
            f"  {_quote_string(metadata_json)} AS metadata, "
            f"  {created_at} AS created_at, "
            f"  {updated_at} AS updated_at"
            f") AS src "
            f"ON target.id = src.id "
            f"WHEN MATCHED THEN UPDATE SET "
            f"  principal_id = src.principal_id, "
            f"  namespace = src.namespace, "
            f"  content = src.content, "
            f"  tags = src.tags, "
            f"  importance = src.importance, "
            f"  embedding = src.embedding, "
            f"  metadata = src.metadata, "
            f"  updated_at = src.updated_at "
            f"WHEN NOT MATCHED THEN INSERT ("
            f"  id, principal_id, namespace, content, tags, importance, "
            f"  embedding, metadata, created_at, updated_at"
            f") VALUES ("
            f"  src.id, src.principal_id, src.namespace, src.content, src.tags, "
            f"  src.importance, src.embedding, src.metadata, src.created_at, "
            f"  src.updated_at"
            f")"
        )

    # -- MemoryStore protocol ----------------------------------------------

    def add(self, memory: Mapping[str, Any]) -> Memory:
        """Insert one memory; embeds ``content`` when ``embedding_fn`` is set."""
        self._ensure_table()
        embedding: list[float] | None = None
        if self._embedding_fn is not None:
            embedding = list(self._embedding_fn([str(memory["content"])])[0])
        materialized = materialize_memory(memory, embedding=embedding)
        self._run_sql(self._merge_sql(materialized))
        return materialized

    def add_batch(self, memories: Sequence[Mapping[str, Any]]) -> list[Memory]:
        """Insert many memories in one embedding call + one MERGE per row.

        Embedding happens in a single batched call so providers that
        amortize per-request overhead see one round-trip. MERGE is one
        statement per row — Delta auto-coalesces under load and keeping
        the SQL small avoids hitting statement-size limits.
        """
        if len(memories) == 0:
            return []
        self._ensure_table()
        if self._embedding_fn is not None:
            embeddings: list[list[float] | None] = [
                list(e) for e in self._embedding_fn([str(m["content"]) for m in memories])
            ]
        else:
            embeddings = [None] * len(memories)
        out: list[Memory] = []
        for raw, emb in zip(memories, embeddings):
            materialized = materialize_memory(raw, embedding=emb)
            self._run_sql(self._merge_sql(materialized))
            out.append(materialized)
        return out

    def get(self, memory_id: str) -> Memory | None:
        """Return the memory with ``memory_id``, or ``None`` if missing."""
        self._ensure_table()
        sql = (
            f"SELECT id, principal_id, namespace, content, tags, importance, "
            f"embedding, metadata, created_at, updated_at "
            f"FROM {self.table_name} "
            f"WHERE id = {_quote_string(memory_id)} LIMIT 1"
        )
        try:
            rows = self._run_sql(sql)
        except Exception as e:
            logger.warning(
                "DeltaMemoryStore.get(%s) failed: %s — returning None.",
                memory_id, e,
            )
            return None
        if not rows:
            return None
        return _row_to_memory(rows[0])

    def update(
        self, memory_id: str, patch: Mapping[str, Any]
    ) -> Memory | None:
        """Apply ``patch`` and MERGE the updated row.

        Re-embeds ``content`` when it appears in the patch and an
        embedder is wired. Returns ``None`` if the row doesn't exist.
        """
        self._ensure_table()
        existing = self.get(memory_id)
        if existing is None:
            return None
        updated = apply_patch(existing, patch)
        if "content" in patch and self._embedding_fn is not None:
            emb = self._embedding_fn([str(patch["content"])])[0]
            updated = Memory(
                id=updated.id,
                principal_id=updated.principal_id,
                namespace=updated.namespace,
                content=updated.content,
                tags=updated.tags,
                importance=updated.importance,
                embedding=tuple(float(v) for v in emb),
                metadata=updated.metadata,
                created_at=updated.created_at,
                updated_at=updated.updated_at,
            )
        self._run_sql(self._merge_sql(updated))
        return updated

    def delete(self, memory_id: str) -> bool:
        """Delete the row; returns ``True`` on hit, ``False`` on miss.

        ``run_sql`` does not surface affected-row counts for a Delta
        ``DELETE``, so existence is checked first (like :meth:`update`).
        Honors the protocol contract that InMemory and Lakebase share.
        """
        self._ensure_table()
        if self.get(memory_id) is None:
            return False
        sql = (
            f"DELETE FROM {self.table_name} "
            f"WHERE id = {_quote_string(memory_id)}"
        )
        try:
            self._run_sql(sql)
            return True
        except Exception as e:
            logger.warning(
                "DeltaMemoryStore.delete(%s) failed: %s", memory_id, e,
            )
            return False

    def list(self, filter: MemoryFilter) -> list[Memory]:
        """SELECT with WHERE filters, ORDER BY updated_at DESC, LIMIT."""
        self._ensure_table()
        where: list[str] = [
            f"principal_id = {_quote_string(filter.principal_id)}"
        ]
        if filter.namespace is not None:
            where.append(f"namespace = {_quote_string(filter.namespace)}")
        if filter.min_importance is not None:
            where.append(f"importance >= {float(filter.min_importance)}")
        if filter.tags is not None and len(filter.tags) > 0:
            arr = _sql_array_str(filter.tags)
            where.append(f"size(array_intersect(tags, {arr})) > 0")
        sql = (
            f"SELECT id, principal_id, namespace, content, tags, importance, "
            f"embedding, metadata, created_at, updated_at "
            f"FROM {self.table_name} "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY updated_at DESC LIMIT {int(filter.limit)}"
        )
        # Let infra failures propagate: a swallowed error here would read
        # as "no memories found" to the recall tool. Reserve [] (an empty
        # result set) for a genuinely empty table. (H21)
        rows = self._run_sql(sql)
        return [_row_to_memory(r) for r in (rows or [])]

    def recall(self, opts: RecallOptions) -> list[RecallResult]:
        """Top-k recall using the 3-tier vector-search-then-cosine-then-recency tree.

        Decision tree:

          * ``vector_search`` + ``embedding_fn``: embed locally, pass
            ``query_vector`` to the index.
          * ``vector_search`` only: pass ``query_text`` to the index.
          * ``embedding_fn`` only: SELECT candidates from the table, score
            with :func:`cosine_similarity`, return top-k.
          * Neither: list candidates and score by recency.
        """
        k = int(opts.k)

        # Branches 1 & 2 — Vector Search delegation.
        if self._vector_search is not None and self._index_name:
            filters: dict[str, Any] = {"principal_id": opts.principal_id}
            if opts.namespace is not None:
                filters["namespace"] = opts.namespace
            columns = [
                "id", "principal_id", "namespace", "content", "tags",
                "importance", "metadata", "created_at", "updated_at",
            ]
            # `tags` / `min_importance` are post-filtered below rather than
            # pushed into the backend filter dict: array-intersection and
            # range semantics vary across Vector Search backends, whereas
            # post-filtering guarantees parity with the SQL / Lakebase
            # paths. Over-fetch so the post-filter still yields up to k. (M12)
            post_filter = (
                opts.tags is not None and len(opts.tags) > 0
            ) or opts.min_importance is not None
            num_results = k * 4 if post_filter else k
            kwargs: dict[str, Any] = {
                "index_name": self._index_name,
                "columns": columns,
                "num_results": num_results,
                "filters": filters,
            }
            if self._embedding_fn is not None:
                kwargs["query_vector"] = list(
                    self._embedding_fn([opts.query])[0]
                )
            else:
                kwargs["query_text"] = opts.query
            result = self._vector_search.similarity_search(**kwargs)
            tag_set = set(opts.tags) if opts.tags else None
            out: list[RecallResult] = []
            for row in result.get("rows") or []:
                memory = _row_to_memory(row)
                # Post-filter to match SQL/Lakebase semantics. (M12)
                if tag_set is not None and not (set(memory.tags) & tag_set):
                    continue
                if (
                    opts.min_importance is not None
                    and memory.importance < float(opts.min_importance)
                ):
                    continue
                # Vector Search backends use _score, score, or
                # similarity_score in practice — fall back gracefully.
                score_val = (
                    row.get("_score")
                    if row.get("_score") is not None
                    else row.get("score")
                    if row.get("score") is not None
                    else row.get("similarity_score")
                    if row.get("similarity_score") is not None
                    else 0.0
                )
                out.append(
                    RecallResult(
                        memory=memory,
                        score=float(score_val or 0.0),
                    )
                )
                if len(out) >= k:
                    break
            return out

        # Branches 3 & 4 — list candidates via SQL.
        candidates = self.list(
            MemoryFilter(
                principal_id=opts.principal_id,
                namespace=opts.namespace,
                tags=opts.tags,
                min_importance=opts.min_importance,
                limit=10_000,
            )
        )

        # Branch 4 — recency fallback.
        if self._embedding_fn is None or len(candidates) == 0:
            n = max(len(candidates), 1)
            return [
                RecallResult(
                    memory=m,
                    score=(0.0 if len(candidates) == 0 else 1 - idx / n),
                )
                for idx, m in enumerate(candidates[:k])
            ]

        # Branch 3 — client-side cosine.
        query_embedding = self._embedding_fn([opts.query])[0]
        scored: list[RecallResult] = []
        for m in candidates:
            if m.embedding is None or len(m.embedding) == 0:
                continue
            scored.append(
                RecallResult(
                    memory=m,
                    score=cosine_similarity(m.embedding, query_embedding),
                )
            )
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:k]


# Force the runtime check at import time — surfaces protocol drift early.
_: type[MemoryStore] = DeltaMemoryStore


__all__ = [
    "DeltaMemoryStore",
    "VectorSearchLike",
]
