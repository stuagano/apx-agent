"""DeltaExampleStore — Unity Catalog Delta-backed :class:`ExampleStore`.

Sibling to :mod:`apx_agent._memory_delta`. Same four-way recall decision
tree (Vector Search with local embed, Vector Search with text, embedded
candidate scan, recency fallback). The discriminating field embedded at
write time is ``input`` — find-similar matches "examples whose inputs look
like this query".

Mirrors ``typescript/src/example-delta.ts``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ._example import (
    Example,
    ExampleFilter,
    ExampleResult,
    ExampleStore,
    FindSimilarOptions,
    apply_example_patch,
    materialize_example,
)
from ._memory import EmbeddingFn, cosine_similarity
from ._memory_delta import (
    VectorSearchLike,
    _epoch_to_iso,
    _iso_to_epoch,
    _quote_string,
    _sql_array_float,
    _sql_array_str,
    _validate_table_name,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def _row_to_example(row: Mapping[str, Any]) -> Example:
    """Convert a raw driver row dict to an :class:`Example` instance.

    Handles JSON-string metadata, list-typed tags/embedding, and nullable
    ``score`` (which is genuinely optional, unlike ``importance`` on
    Memory).
    """
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

    score_raw = row.get("score")
    score: float | None = float(score_raw) if score_raw is not None else None

    return Example(
        id=str(row.get("id") or ""),
        agent_id=str(row.get("agent_id") or ""),
        intent=str(row.get("intent") or "default"),
        input=str(row.get("input") or ""),
        output=str(row.get("output") or ""),
        score=score,
        tags=tags,
        embedding=embedding,
        metadata=metadata,
        created_at=_epoch_to_iso(row.get("created_at")),
        updated_at=_epoch_to_iso(row.get("updated_at")),
    )


# ---------------------------------------------------------------------------
# DeltaExampleStore
# ---------------------------------------------------------------------------


class DeltaExampleStore:
    """Delta-backed :class:`ExampleStore` implementation."""

    def __init__(
        self,
        *,
        run_sql: Callable[[str], list[dict]],
        embedding_fn: EmbeddingFn | None = None,
        embedding_dim: int | None = None,
        table_name: str = "apx_examples",
        auto_create: bool = True,
        vector_search: VectorSearchLike | None = None,
        index_name: str | None = None,
    ) -> None:
        """Construct a Delta-backed example store.

        Args:
            run_sql: Callable that executes SQL and returns row dicts.
            embedding_fn: Optional batched embedding function. When set,
                each example's ``input`` is embedded on write and query
                strings are embedded on :meth:`find_similar`.
            embedding_dim: Informational dimensionality marker.
            table_name: Fully-qualified UC table name. Validated against
                a safe-identifier allowlist.
            auto_create: Issue ``CREATE TABLE IF NOT EXISTS`` on first
                use. Set False when provisioning is out-of-band.
            vector_search: Optional :class:`VectorSearchLike` delegate.
            index_name: Vector Search index name. Required when
                ``vector_search`` is provided.
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
        """Idempotently create the Delta table on first use."""
        if self._created or not self._auto_create:
            return
        sql = (
            f"CREATE TABLE IF NOT EXISTS {self.table_name} ("
            f"  id STRING,"
            f"  agent_id STRING,"
            f"  intent STRING,"
            f"  input STRING,"
            f"  output STRING,"
            f"  score FLOAT,"
            f"  tags ARRAY<STRING>,"
            f"  embedding ARRAY<FLOAT>,"
            f"  metadata STRING,"
            f"  created_at DOUBLE,"
            f"  updated_at DOUBLE"
            f") USING DELTA"
        )
        try:
            self._run_sql(sql)
        except Exception as e:
            schema_parts = self.table_name.rsplit(".", 1)[0]
            logger.warning(
                "DeltaExampleStore: CREATE TABLE IF NOT EXISTS %s failed: %s\n"
                "  Fix: GRANT CREATE TABLE ON SCHEMA %s TO `<your-principal>`;\n"
                "  Or pre-create the table and set auto_create=False.",
                self.table_name, e, schema_parts,
            )
            return  # don't set _created; let the next call retry
        self._created = True

    # -- MERGE SQL ----------------------------------------------------------

    def _merge_sql(self, example: Example) -> str:
        """Build the ``MERGE INTO`` upsert SQL for one example row."""
        created_at = _iso_to_epoch(example.created_at)
        updated_at = _iso_to_epoch(example.updated_at)
        metadata_json = json.dumps(dict(example.metadata or {}), default=str)
        if example.score is None:
            score_lit = "CAST(NULL AS FLOAT)"
        else:
            score_lit = f"CAST({float(example.score)} AS FLOAT)"
        return (
            f"MERGE INTO {self.table_name} AS target "
            f"USING (SELECT "
            f"  {_quote_string(example.id)} AS id, "
            f"  {_quote_string(example.agent_id)} AS agent_id, "
            f"  {_quote_string(example.intent)} AS intent, "
            f"  {_quote_string(example.input)} AS input, "
            f"  {_quote_string(example.output)} AS output, "
            f"  {score_lit} AS score, "
            f"  {_sql_array_str(example.tags)} AS tags, "
            f"  {_sql_array_float(example.embedding)} AS embedding, "
            f"  {_quote_string(metadata_json)} AS metadata, "
            f"  {created_at} AS created_at, "
            f"  {updated_at} AS updated_at"
            f") AS src "
            f"ON target.id = src.id "
            f"WHEN MATCHED THEN UPDATE SET "
            f"  agent_id = src.agent_id, "
            f"  intent = src.intent, "
            f"  input = src.input, "
            f"  output = src.output, "
            f"  score = src.score, "
            f"  tags = src.tags, "
            f"  embedding = src.embedding, "
            f"  metadata = src.metadata, "
            f"  updated_at = src.updated_at "
            f"WHEN NOT MATCHED THEN INSERT ("
            f"  id, agent_id, intent, input, output, score, tags, embedding, "
            f"  metadata, created_at, updated_at"
            f") VALUES ("
            f"  src.id, src.agent_id, src.intent, src.input, src.output, "
            f"  src.score, src.tags, src.embedding, src.metadata, "
            f"  src.created_at, src.updated_at"
            f")"
        )

    # -- ExampleStore protocol ---------------------------------------------

    def add(self, example: Mapping[str, Any]) -> Example:
        """Insert one example; embeds ``input`` when an embedder is wired."""
        self._ensure_table()
        embedding: list[float] | None = None
        if self._embedding_fn is not None:
            embedding = list(self._embedding_fn([str(example["input"])])[0])
        materialized = materialize_example(example, embedding=embedding)
        self._run_sql(self._merge_sql(materialized))
        return materialized

    def add_batch(
        self, examples: Sequence[Mapping[str, Any]]
    ) -> list[Example]:
        """Insert many examples; single batched embedding call."""
        if len(examples) == 0:
            return []
        self._ensure_table()
        if self._embedding_fn is not None:
            embeddings: list[list[float] | None] = [
                list(e) for e in self._embedding_fn(
                    [str(x["input"]) for x in examples]
                )
            ]
        else:
            embeddings = [None] * len(examples)
        out: list[Example] = []
        for raw, emb in zip(examples, embeddings):
            materialized = materialize_example(raw, embedding=emb)
            self._run_sql(self._merge_sql(materialized))
            out.append(materialized)
        return out

    def get(self, example_id: str) -> Example | None:
        """Return the example with ``example_id``, or ``None`` if missing."""
        self._ensure_table()
        sql = (
            f"SELECT id, agent_id, intent, input, output, score, tags, "
            f"embedding, metadata, created_at, updated_at "
            f"FROM {self.table_name} "
            f"WHERE id = {_quote_string(example_id)} LIMIT 1"
        )
        try:
            rows = self._run_sql(sql)
        except Exception as e:
            logger.warning(
                "DeltaExampleStore.get(%s) failed: %s", example_id, e,
            )
            return None
        if not rows:
            return None
        return _row_to_example(rows[0])

    def update(
        self, example_id: str, patch: Mapping[str, Any]
    ) -> Example | None:
        """Apply ``patch`` and MERGE the updated row.

        Re-embeds ``input`` when it appears in the patch.
        """
        self._ensure_table()
        existing = self.get(example_id)
        if existing is None:
            return None
        updated = apply_example_patch(existing, patch)
        if "input" in patch and self._embedding_fn is not None:
            emb = self._embedding_fn([str(patch["input"])])[0]
            updated = Example(
                id=updated.id,
                agent_id=updated.agent_id,
                intent=updated.intent,
                input=updated.input,
                output=updated.output,
                score=updated.score,
                tags=updated.tags,
                embedding=tuple(float(v) for v in emb),
                metadata=updated.metadata,
                created_at=updated.created_at,
                updated_at=updated.updated_at,
            )
        self._run_sql(self._merge_sql(updated))
        return updated

    def delete(self, example_id: str) -> bool:
        """Delete the row; returns True on hit, False on miss.

        Honors the ``ExampleStore`` contract (True only when a row existed),
        matching the InMemory and Lakebase stores. Existence is checked via
        ``get`` first, since ``run_sql`` does not reliably surface an
        affected-rows count.
        """
        self._ensure_table()
        existing = self.get(example_id)
        if existing is None:
            return False
        sql = (
            f"DELETE FROM {self.table_name} "
            f"WHERE id = {_quote_string(example_id)}"
        )
        try:
            self._run_sql(sql)
            return True
        except Exception as e:
            logger.warning(
                "DeltaExampleStore.delete(%s) failed: %s", example_id, e,
            )
            return False

    def list(self, filter: ExampleFilter) -> list[Example]:
        """SELECT with WHERE filters, ORDER BY updated_at DESC, LIMIT."""
        self._ensure_table()
        where: list[str] = [
            f"agent_id = {_quote_string(filter.agent_id)}"
        ]
        if filter.intent is not None:
            where.append(f"intent = {_quote_string(filter.intent)}")
        if filter.min_score is not None:
            where.append(f"score >= {float(filter.min_score)}")
        if filter.tags is not None and len(filter.tags) > 0:
            arr = _sql_array_str(filter.tags)
            where.append(f"size(array_intersect(tags, {arr})) > 0")
        sql = (
            f"SELECT id, agent_id, intent, input, output, score, tags, "
            f"embedding, metadata, created_at, updated_at "
            f"FROM {self.table_name} "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY updated_at DESC LIMIT {int(filter.limit)}"
        )
        # Let infra failures propagate: a swallowed error would read as "no
        # examples" and silently degrade answer quality — reserve [] for a
        # genuinely empty result. (matches the memory store, H21) (#380)
        rows = self._run_sql(sql)
        return [_row_to_example(r) for r in (rows or [])]

    def find_similar(self, opts: FindSimilarOptions) -> list[ExampleResult]:
        """Top-k retrieval via the 3-tier fallback (see :class:`DeltaMemoryStore`)."""
        k = int(opts.k)

        # Branches 1 & 2 — Vector Search delegation.
        if self._vector_search is not None and self._index_name:
            filters: dict[str, Any] = {"agent_id": opts.agent_id}
            if opts.intent is not None:
                filters["intent"] = opts.intent
            columns = [
                "id", "agent_id", "intent", "input", "output", "score",
                "tags", "metadata", "created_at", "updated_at",
            ]
            kwargs: dict[str, Any] = {
                "index_name": self._index_name,
                "columns": columns,
                "num_results": k,
                "filters": filters,
            }
            if self._embedding_fn is not None:
                kwargs["query_vector"] = list(
                    self._embedding_fn([opts.query])[0]
                )
            else:
                kwargs["query_text"] = opts.query
            result = self._vector_search.similarity_search(**kwargs)
            # Vector Search filters only express ``agent_id``/``intent``
            # cleanly here, so honor ``tags``/``min_score`` by post-filtering
            # the returned rows (the SQL and Lakebase paths apply them too).
            # ``min_score`` matches the store-side ``score >= min_score``
            # semantics: rows with a NULL score are excluded.
            tag_filter = set(opts.tags) if opts.tags else None
            out: list[ExampleResult] = []
            for row in result.get("rows") or []:
                example = _row_to_example(row)
                if opts.min_score is not None and (
                    example.score is None or example.score < opts.min_score
                ):
                    continue
                if tag_filter is not None and not (
                    tag_filter & set(example.tags)
                ):
                    continue
                # NOTE: ``score`` is BOTH a vector-search rank field and a
                # genuine Example column. Prefer ``_score`` when the
                # backend sets it (it's the standard Databricks shape);
                # otherwise fall through to ``similarity_score``. Don't
                # use ``score`` here — it would collide with the row's
                # own quality-score column.
                score_val = (
                    row.get("_score")
                    if row.get("_score") is not None
                    else row.get("similarity_score")
                    if row.get("similarity_score") is not None
                    else 0.0
                )
                out.append(
                    ExampleResult(
                        example=example,
                        score=float(score_val or 0.0),
                    )
                )
            return out

        # Branches 3 & 4 — list candidates via SQL.
        candidates = self.list(
            ExampleFilter(
                agent_id=opts.agent_id,
                intent=opts.intent,
                tags=opts.tags,
                min_score=opts.min_score,
                limit=10_000,
            )
        )

        # Branch 4 — recency fallback.
        if self._embedding_fn is None or len(candidates) == 0:
            n = max(len(candidates), 1)
            return [
                ExampleResult(
                    example=ex,
                    score=(0.0 if len(candidates) == 0 else 1 - idx / n),
                )
                for idx, ex in enumerate(candidates[:k])
            ]

        # Branch 3 — client-side cosine.
        query_embedding = self._embedding_fn([opts.query])[0]
        scored: list[ExampleResult] = []
        for ex in candidates:
            if ex.embedding is None or len(ex.embedding) == 0:
                continue
            scored.append(
                ExampleResult(
                    example=ex,
                    score=cosine_similarity(ex.embedding, query_embedding),
                )
            )
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:k]


# Force the runtime check at import time.
_: type[ExampleStore] = DeltaExampleStore


__all__ = [
    "DeltaExampleStore",
]
