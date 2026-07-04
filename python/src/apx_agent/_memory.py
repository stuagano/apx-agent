"""memory — durable, semantically-retrievable memory for agents.

Sibling pattern to ``_session.py``:

  * Sessions persist per-conversation history + state.
  * Memories persist per-principal long-lived facts/observations across
    conversations, with semantic recall.

A :class:`MemoryStore` is keyed by ``principal_id`` (user, customer, agent —
whatever identity the caller chooses) and ``namespace`` (e.g. ``"profile"``,
``"episodic"``, ``"semantic"``, or any custom bucket the agent designates).

Recall is **vector-based by default**. When an :data:`EmbeddingFn` is wired
into the store, :meth:`MemoryStore.add` embeds the content at write time and
:meth:`MemoryStore.recall` embeds the query at read time and ranks by cosine
similarity. Stores that don't have an embedder (or callers that don't want
semantic recall) can pass ``embedding_fn=None`` — ``recall`` then degrades
to tag-filtered chronological recency.

Same injectable-dependency philosophy as the rest of the framework: no
``databricks-embeddings`` import inside the package; the caller wires the
actual embedding source (FMAPI, Vector Search endpoint, OpenAI, a local
model, a test fake) via the :data:`EmbeddingFn` callable.

Mirrors ``typescript/src/memory.ts`` — same field names (snake_case here,
camelCase there), same semantics.
"""

from __future__ import annotations

import math
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


EmbeddingFn = Callable[[Sequence[str]], list[list[float]]]
"""Injectable embedding callable.

Stores accept a batched embedding function so they can compute embeddings
for a list of memories on bulk-add or for a single query on recall. The
implementation may proxy to Databricks Foundation Model APIs, an OpenAI
client, a local model, or a test fake.

Embedding dimensionality is determined by the function. Stores that
persist embeddings (Lakebase pgvector) must be configured with the same
``embedding_dim`` the function actually returns.
"""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Memory:
    """A single durable memory. Equivalent to a row in the store.

    Attributes:
        id: Stable identifier for the memory. Mintable by the caller; falls
            back to a UUID prefixed with ``mem_``.
        principal_id: Scoping key — usually a user ID, customer ID, or agent
            ID.
        namespace: Free-form bucket the agent chooses (e.g. ``"profile"``,
            ``"preference"``, ``"episodic"``).
        content: The memory text. Embedded at write time when an
            ``embedding_fn`` is wired into the store.
        tags: Tuple of tag strings for finer-grained filtering inside a
            namespace.
        importance: 0..1 scalar the caller can use for ranking,
            decay-over-time, or pruning. The store does not interpret it —
            it is just stored.
        embedding: Present when the store is backed by a vector backend
            AND an ``embedding_fn`` was wired in. Callers should not depend
            on its shape — it's an implementation detail of the store.
        metadata: Caller-supplied opaque metadata dict.
        created_at: ISO-8601 timestamp at first insertion.
        updated_at: ISO-8601 timestamp at the most recent mutation.
    """

    id: str
    principal_id: str
    namespace: str
    content: str
    tags: tuple[str, ...]
    importance: float
    embedding: tuple[float, ...] | None
    metadata: Mapping[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RecallResult:
    """A recall result — the memory plus its similarity score.

    Attributes:
        memory: The :class:`Memory` row.
        score: Cosine similarity in 0..1 when vector recall ran. When the
            store has no embedder (recency fallback), ``score`` is the
            chronological position normalized to 0..1, newer = higher.
    """

    memory: Memory
    score: float


@dataclass(frozen=True)
class MemoryFilter:
    """Filter shape for :meth:`MemoryStore.list`.

    Attributes:
        principal_id: Required — scopes the listing to one principal.
        namespace: Optional exact-match namespace filter.
        tags: Optional any-of tag filter; rows whose tags overlap with this
            set pass.
        min_importance: Optional minimum-importance threshold.
        limit: Max rows. Defaults to 100.
    """

    principal_id: str
    namespace: str | None = None
    tags: tuple[str, ...] | None = None
    min_importance: float | None = None
    limit: int = 100


@dataclass(frozen=True)
class RecallOptions:
    """Options for :meth:`MemoryStore.recall`.

    Attributes:
        principal_id: Required — scopes recall to one principal.
        query: The natural-language query that gets embedded.
        namespace: Optional exact-match namespace filter.
        tags: Optional any-of tag filter.
        min_importance: Optional minimum-importance threshold.
        k: Top-k. Defaults to 5.
    """

    principal_id: str
    query: str
    namespace: str | None = None
    tags: tuple[str, ...] | None = None
    min_importance: float | None = None
    k: int = 5


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryStore(Protocol):
    """Storage backend for :class:`Memory` rows.

    Implementations may be process-local (:class:`InMemoryMemoryStore`),
    Lakebase-pgvector-backed (``LakebaseMemoryStore``), or anything else
    that satisfies this contract.

    All methods are synchronous to match ``_session.py`` style; backends
    that wrap async transports should adapt at the boundary.
    """

    def add(self, memory: Mapping[str, Any]) -> Memory:
        """Insert one memory; returns the materialized row."""
        ...

    def add_batch(self, memories: Sequence[Mapping[str, Any]]) -> list[Memory]:
        """Insert many memories; returns materialized rows in input order."""
        ...

    def get(self, memory_id: str) -> Memory | None:
        """Return the memory with the given id, or ``None`` if missing."""
        ...

    def update(self, memory_id: str, patch: Mapping[str, Any]) -> Memory | None:
        """Apply a patch and return the updated row, or ``None`` if missing."""
        ...

    def delete(self, memory_id: str) -> bool:
        """Delete the row; returns ``True`` on hit, ``False`` on miss."""
        ...

    def list(self, filter: MemoryFilter) -> list[Memory]:
        """Return rows matching ``filter``, sorted by ``updated_at`` desc."""
        ...

    def recall(self, opts: RecallOptions) -> list[RecallResult]:
        """Semantic recall via cosine similarity, with optional filters."""
        ...


# ---------------------------------------------------------------------------
# Pure helpers exported for backends
# ---------------------------------------------------------------------------


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two vectors.

    Returns 0 when either is empty, dimensions mismatch, or either is
    all-zero. Used by :class:`InMemoryMemoryStore` and exported so other
    backends with client-side ranking share the same math.
    """
    if len(a) == 0 or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def tags_overlap(
    memory_tags: Iterable[str],
    filter_tags: Sequence[str] | None,
) -> bool:
    """Return ``True`` when the memory has at least one of the filter tags.

    Empty or ``None`` filter matches everything (any-of semantics).
    """
    if not filter_tags:
        return True
    memory_set = set(memory_tags)
    for t in filter_tags:
        if t in memory_set:
            return True
    return False


def iso_now() -> str:
    """ISO-8601 UTC timestamp with millisecond precision and trailing ``Z``.

    Matches the TS ``new Date().toISOString()`` format so memories created
    by the Python and TS clients sort identically when stringified.
    """
    now = datetime.now(timezone.utc)
    # Format: YYYY-MM-DDTHH:MM:SS.sssZ
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def iso_to_epoch(iso: str | None) -> float:
    """Convert ISO-8601 to UNIX epoch seconds. Returns 0 on garbage."""
    if not iso:
        return 0.0
    try:
        s = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


_TABLE_NAME_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_."
)


def validate_table_name(name: str) -> None:
    """Reject table names that can't be a safe SQL identifier.

    ``table_name`` is interpolated raw into statements (neither UC nor
    Postgres bind parameters can stand in for identifiers), so it must be
    constrained to a strict allowlist: alnum, underscore, and dot (for a
    ``catalog.schema.table`` prefix). Anything else risks SQL injection.
    """
    if not name:
        raise ValueError("table_name must not be empty")
    for ch in name:
        if ch not in _TABLE_NAME_ALLOWED:
            raise ValueError(
                f"table_name contains illegal character {ch!r}: {name!r}"
            )


def new_memory_id(prefix: str = "mem") -> str:
    """Generate a memory id of the form ``"{prefix}_{uuid4}"``."""
    return f"{prefix}_{uuid.uuid4()}"


def materialize_memory(
    input_data: Mapping[str, Any],
    embedding: Sequence[float] | None = None,
) -> Memory:
    """Normalize a raw input dict to a fully-populated :class:`Memory`.

    Backends call this to centralize defaulting + id minting. ``input_data``
    must contain ``principal_id`` and ``content``; other fields are
    optional and default per the type spec.
    """
    now = iso_now()
    return Memory(
        id=str(input_data.get("id") or new_memory_id()),
        principal_id=str(input_data["principal_id"]),
        namespace=str(input_data.get("namespace") or "default"),
        content=str(input_data["content"]),
        tags=tuple(input_data.get("tags") or ()),
        importance=(
            float(input_data["importance"])
            if input_data.get("importance") is not None
            else 0.5
        ),
        embedding=tuple(float(v) for v in embedding) if embedding is not None else None,
        metadata=dict(input_data.get("metadata") or {}),
        created_at=now,
        updated_at=now,
    )


def apply_patch(memory: Memory, patch: Mapping[str, Any]) -> Memory:
    """Apply a patch to an existing memory and bump ``updated_at``.

    Only the mutable surface (``content``, ``tags``, ``importance``,
    ``metadata``, ``namespace``) is honored. Unknown keys are ignored.
    """
    return Memory(
        id=memory.id,
        principal_id=memory.principal_id,
        namespace=(
            str(patch["namespace"]) if "namespace" in patch else memory.namespace
        ),
        content=str(patch["content"]) if "content" in patch else memory.content,
        tags=tuple(patch["tags"]) if "tags" in patch else memory.tags,
        importance=(
            float(patch["importance"]) if "importance" in patch else memory.importance
        ),
        embedding=memory.embedding,
        metadata=(
            dict(patch["metadata"]) if "metadata" in patch else memory.metadata
        ),
        created_at=memory.created_at,
        updated_at=iso_now(),
    )


# ---------------------------------------------------------------------------
# InMemoryMemoryStore — baseline for dev, tests, and fallback
# ---------------------------------------------------------------------------


class InMemoryMemoryStore:
    """In-process :class:`MemoryStore` backed by a dict.

    Cosine-similarity recall when an embedder is wired, recency fallback
    otherwise. Data is lost on restart. Suitable for tests and local agent
    development.
    """

    def __init__(self, *, embedding_fn: EmbeddingFn | None = None) -> None:
        """Construct an in-memory store.

        Args:
            embedding_fn: Optional batched embedding function. When provided,
                ``add`` embeds ``content`` and ``recall`` embeds the query
                and ranks by cosine similarity. When omitted, ``recall``
                degrades to recency-ordered results.
        """
        self._embedding_fn = embedding_fn
        self._rows: dict[str, Memory] = {}
        # The store is a process singleton shared across concurrent to_thread
        # request workers; every dict mutation/iteration is guarded so a write
        # can't race an in-flight list()/recall() scan (dict-changed-size crash),
        # matching InMemoryConversationStore. recall() holds no lock — it goes
        # through list(), which does, then works on that snapshot.
        self._lock = threading.Lock()

    def add(self, memory: Mapping[str, Any]) -> Memory:
        """Insert one memory; returns the materialized row."""
        embedding: list[float] | None = None
        if self._embedding_fn is not None:
            embedding = self._embedding_fn([str(memory["content"])])[0]
        materialized = materialize_memory(memory, embedding=embedding)
        with self._lock:
            self._rows[materialized.id] = materialized
        return materialized

    def add_batch(self, memories: Sequence[Mapping[str, Any]]) -> list[Memory]:
        """Insert many memories; returns materialized rows in input order."""
        if len(memories) == 0:
            return []
        embeddings: list[list[float] | None]
        if self._embedding_fn is not None:
            embeddings = list(
                self._embedding_fn([str(m["content"]) for m in memories])
            )
        else:
            embeddings = [None] * len(memories)
        out: list[Memory] = []
        materialized_rows = [
            materialize_memory(raw, embedding=emb)
            for raw, emb in zip(memories, embeddings)
        ]
        with self._lock:
            for materialized in materialized_rows:
                self._rows[materialized.id] = materialized
                out.append(materialized)
        return out

    def get(self, memory_id: str) -> Memory | None:
        """Return the memory with ``memory_id``, or ``None`` if missing."""
        with self._lock:
            return self._rows.get(memory_id)

    def update(self, memory_id: str, patch: Mapping[str, Any]) -> Memory | None:
        """Apply ``patch`` and return the updated row, or ``None`` if missing."""
        with self._lock:
            existing = self._rows.get(memory_id)
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
            self._rows[memory_id] = updated
            return updated

    def delete(self, memory_id: str) -> bool:
        """Delete the row; returns ``True`` on hit, ``False`` on miss."""
        with self._lock:
            return self._rows.pop(memory_id, None) is not None

    def list(self, filter: MemoryFilter) -> list[Memory]:
        """Return rows matching ``filter``, newest first, capped at ``limit``."""
        out: list[Memory] = []
        with self._lock:
            rows = list(self._rows.values())
        for m in rows:
            if m.principal_id != filter.principal_id:
                continue
            if filter.namespace is not None and m.namespace != filter.namespace:
                continue
            if not tags_overlap(m.tags, filter.tags):
                continue
            if (
                filter.min_importance is not None
                and m.importance < filter.min_importance
            ):
                continue
            out.append(m)
        out.sort(key=lambda m: m.updated_at, reverse=True)
        return out[: filter.limit]

    def recall(self, opts: RecallOptions) -> list[RecallResult]:
        """Top-k recall under ``opts``.

        With an embedder, ranks by cosine similarity to the embedded query.
        Without an embedder, returns the most-recently-updated rows with a
        normalized recency score.
        """
        candidates = self.list(
            MemoryFilter(
                principal_id=opts.principal_id,
                namespace=opts.namespace,
                tags=opts.tags,
                min_importance=opts.min_importance,
                limit=10_000,
            )
        )

        if self._embedding_fn is None or len(candidates) == 0:
            n = max(len(candidates), 1)
            return [
                RecallResult(
                    memory=memory,
                    score=(0.0 if len(candidates) == 0 else 1 - idx / n),
                )
                for idx, memory in enumerate(candidates[: opts.k])
            ]

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
        return scored[: opts.k]


__all__ = [
    "EmbeddingFn",
    "InMemoryMemoryStore",
    "Memory",
    "MemoryFilter",
    "MemoryStore",
    "RecallOptions",
    "RecallResult",
    "apply_patch",
    "cosine_similarity",
    "iso_now",
    "materialize_memory",
    "new_memory_id",
    "tags_overlap",
]
# `field` imported for forward compatibility with future mutable defaults
_ = field
