"""example — durable, semantically-retrievable few-shot examples for agents.

Sibling to ``_memory.py``. Where :class:`MemoryStore` persists per-principal
observations, :class:`ExampleStore` persists per-agent input/output exemplars
used for in-context learning, prompt assembly, and quality reference.

Recall is **vector-based by default**, same pattern as memory: an
:data:`EmbeddingFn` is wired into the store; :meth:`ExampleStore.add` embeds
the example's ``input`` (the discriminating field — what the LLM compares the
incoming query against) at write time, and :meth:`ExampleStore.find_similar`
embeds the query and ranks by cosine similarity.

Scope key is ``agent_id`` — examples are bound to a specific agent (typically
the supervisor or a sub-agent's logical name). ``intent`` is a free-form bucket
the agent uses to partition the example space (e.g. ``"classify_intent"``,
``"extract_fields"``, ``"final_answer"``).

Mirrors ``typescript/src/example.ts`` — same field names (snake_case here,
camelCase there), same semantics.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ._memory import EmbeddingFn, cosine_similarity, iso_now, tags_overlap

# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Example:
    """A single few-shot example.

    ``input`` is what an incoming query is compared against; ``output`` is the
    demonstration response. ``score`` is a 0..1 quality signal (human rating,
    eval result, etc.). ``tags`` allow finer-grained filtering inside an
    intent.

    ``embedding`` is the vector of ``input`` (not of input+output), so recall
    matches "examples whose inputs look like this query".
    """

    id: str
    agent_id: str
    intent: str
    input: str
    output: str
    score: float | None
    tags: tuple[str, ...]
    embedding: tuple[float, ...] | None
    metadata: Mapping[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ExampleResult:
    """A retrieval result — the example plus its similarity score."""

    example: Example
    score: float


@dataclass(frozen=True)
class ExampleFilter:
    """Filter shape for :meth:`ExampleStore.list`.

    Attributes:
        agent_id: Required — scopes the listing to one agent.
        intent: Optional exact-match intent filter.
        tags: Optional any-of tag filter.
        min_score: Optional minimum-score threshold. Rows with no score are
            excluded when this is set.
        limit: Max rows. Defaults to 100.
    """

    agent_id: str
    intent: str | None = None
    tags: tuple[str, ...] | None = None
    min_score: float | None = None
    limit: int = 100


@dataclass(frozen=True)
class FindSimilarOptions:
    """Options for :meth:`ExampleStore.find_similar`.

    Attributes:
        agent_id: Required — scopes recall to one agent.
        query: The natural-language query that gets embedded.
        intent: Optional exact-match intent filter.
        tags: Optional any-of tag filter.
        min_score: Optional minimum-score threshold.
        k: Top-k. Defaults to 5.
    """

    agent_id: str
    query: str
    intent: str | None = None
    tags: tuple[str, ...] | None = None
    min_score: float | None = None
    k: int = 5


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ExampleStore(Protocol):
    """Storage backend for :class:`Example` rows.

    Implementations may be process-local (:class:`InMemoryExampleStore`),
    Lakebase-pgvector-backed (``LakebaseExampleStore``), or anything else
    that satisfies this contract.
    """

    def add(self, example: Mapping[str, Any]) -> Example:
        """Insert one example; returns the materialized row."""
        ...

    def add_batch(self, examples: Sequence[Mapping[str, Any]]) -> list[Example]:
        """Insert many examples; returns materialized rows in input order."""
        ...

    def get(self, example_id: str) -> Example | None:
        """Return the example with the given id, or ``None`` if missing."""
        ...

    def update(self, example_id: str, patch: Mapping[str, Any]) -> Example | None:
        """Apply a patch and return the updated row, or ``None`` if missing."""
        ...

    def delete(self, example_id: str) -> bool:
        """Delete the row; returns ``True`` on hit, ``False`` on miss."""
        ...

    def list(self, filter: ExampleFilter) -> list[Example]:
        """Return rows matching ``filter``, sorted by ``updated_at`` desc."""
        ...

    def find_similar(self, opts: FindSimilarOptions) -> list[ExampleResult]:
        """Semantic retrieval via cosine similarity, with optional filters."""
        ...


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def new_example_id(prefix: str = "ex") -> str:
    """Generate an example id of the form ``"{prefix}_{uuid4}"``."""
    return f"{prefix}_{uuid.uuid4()}"


def materialize_example(
    input_data: Mapping[str, Any],
    embedding: Sequence[float] | None = None,
) -> Example:
    """Normalize a raw input dict to a fully-populated :class:`Example`.

    ``input_data`` must contain ``agent_id``, ``input``, and ``output``;
    other fields are optional and default per the type spec.
    """
    now = iso_now()
    score_raw = input_data.get("score")
    return Example(
        id=str(input_data.get("id") or new_example_id()),
        agent_id=str(input_data["agent_id"]),
        intent=str(input_data.get("intent") or "default"),
        input=str(input_data["input"]),
        output=str(input_data["output"]),
        score=float(score_raw) if score_raw is not None else None,
        tags=tuple(input_data.get("tags") or ()),
        embedding=tuple(float(v) for v in embedding) if embedding is not None else None,
        metadata=dict(input_data.get("metadata") or {}),
        created_at=now,
        updated_at=now,
    )


def apply_example_patch(example: Example, patch: Mapping[str, Any]) -> Example:
    """Apply a patch to an existing example and bump ``updated_at``.

    Only the mutable surface (``input``, ``output``, ``score``, ``tags``,
    ``metadata``, ``intent``) is honored. Unknown keys are ignored.
    """
    return Example(
        id=example.id,
        agent_id=example.agent_id,
        intent=str(patch["intent"]) if "intent" in patch else example.intent,
        input=str(patch["input"]) if "input" in patch else example.input,
        output=str(patch["output"]) if "output" in patch else example.output,
        score=(
            (float(patch["score"]) if patch["score"] is not None else None)
            if "score" in patch
            else example.score
        ),
        tags=tuple(patch["tags"]) if "tags" in patch else example.tags,
        embedding=example.embedding,
        metadata=(
            dict(patch["metadata"]) if "metadata" in patch else example.metadata
        ),
        created_at=example.created_at,
        updated_at=iso_now(),
    )


# ---------------------------------------------------------------------------
# InMemoryExampleStore — baseline
# ---------------------------------------------------------------------------


class InMemoryExampleStore:
    """In-process :class:`ExampleStore` backed by a dict.

    Cosine-similarity retrieval when an embedder is wired, recency fallback
    otherwise. Data is lost on restart. Suitable for tests and local agent
    development.
    """

    def __init__(self, *, embedding_fn: EmbeddingFn | None = None) -> None:
        """Construct an in-memory example store.

        Args:
            embedding_fn: Optional batched embedding function. When provided,
                ``add`` embeds the ``input`` field and ``find_similar``
                embeds the query and ranks by cosine similarity.
        """
        self._embedding_fn = embedding_fn
        self._rows: dict[str, Example] = {}
        # Guards every dict mutation/iteration so a write can't race an in-flight
        # list()/find_similar() scan under concurrent request workers (dict-
        # changed-size crash). find_similar() holds no lock — it goes through
        # list(), which does, then works on that snapshot.
        self._lock = threading.Lock()

    def add(self, example: Mapping[str, Any]) -> Example:
        """Insert one example; returns the materialized row."""
        embedding: list[float] | None = None
        if self._embedding_fn is not None:
            embedding = self._embedding_fn([str(example["input"])])[0]
        materialized = materialize_example(example, embedding=embedding)
        with self._lock:
            self._rows[materialized.id] = materialized
        return materialized

    def add_batch(self, examples: Sequence[Mapping[str, Any]]) -> list[Example]:
        """Insert many examples; returns materialized rows in input order."""
        if len(examples) == 0:
            return []
        embeddings: list[list[float] | None]
        if self._embedding_fn is not None:
            embeddings = list(
                self._embedding_fn([str(e["input"]) for e in examples])
            )
        else:
            embeddings = [None] * len(examples)
        out: list[Example] = []
        materialized_rows = [
            materialize_example(raw, embedding=emb)
            for raw, emb in zip(examples, embeddings)
        ]
        with self._lock:
            for materialized in materialized_rows:
                self._rows[materialized.id] = materialized
                out.append(materialized)
        return out

    def get(self, example_id: str) -> Example | None:
        """Return the example with ``example_id``, or ``None`` if missing."""
        with self._lock:
            return self._rows.get(example_id)

    def update(self, example_id: str, patch: Mapping[str, Any]) -> Example | None:
        """Apply ``patch`` and return the updated row, or ``None`` if missing."""
        with self._lock:
            existing = self._rows.get(example_id)
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
            self._rows[example_id] = updated
            return updated

    def delete(self, example_id: str) -> bool:
        """Delete the row; returns ``True`` on hit, ``False`` on miss."""
        with self._lock:
            return self._rows.pop(example_id, None) is not None

    def list(self, filter: ExampleFilter) -> list[Example]:
        """Return rows matching ``filter``, newest first, capped at ``limit``."""
        out: list[Example] = []
        with self._lock:
            rows = list(self._rows.values())
        for ex in rows:
            if ex.agent_id != filter.agent_id:
                continue
            if filter.intent is not None and ex.intent != filter.intent:
                continue
            if not tags_overlap(ex.tags, filter.tags):
                continue
            if filter.min_score is not None:
                if ex.score is None or ex.score < filter.min_score:
                    continue
            out.append(ex)
        out.sort(key=lambda e: e.updated_at, reverse=True)
        return out[: filter.limit]

    def find_similar(self, opts: FindSimilarOptions) -> list[ExampleResult]:
        """Top-k retrieval under ``opts``."""
        candidates = self.list(
            ExampleFilter(
                agent_id=opts.agent_id,
                intent=opts.intent,
                tags=opts.tags,
                min_score=opts.min_score,
                limit=10_000,
            )
        )

        if self._embedding_fn is None or len(candidates) == 0:
            n = max(len(candidates), 1)
            return [
                ExampleResult(
                    example=ex,
                    score=(0.0 if len(candidates) == 0 else 1 - idx / n),
                )
                for idx, ex in enumerate(candidates[: opts.k])
            ]

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
        return scored[: opts.k]


__all__ = [
    "Example",
    "ExampleFilter",
    "ExampleResult",
    "ExampleStore",
    "FindSimilarOptions",
    "InMemoryExampleStore",
    "apply_example_patch",
    "cosine_similarity",
    "iso_now",
    "materialize_example",
    "new_example_id",
    "tags_overlap",
]
