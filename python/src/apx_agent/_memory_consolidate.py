"""memory-consolidate — LLM-backed summarization of older memories.

Summarize a principal's older memories via a caller-supplied
``summarize_fn`` and write back a single consolidated :class:`Memory`,
optionally deleting the originals. Keeps a memory bank tractable as it
grows.

Typical recipe:

  1. Run periodically per principal.
  2. Pull memories older than N seconds (``max_age_seconds``).
  3. Optionally floor on ``min_importance`` to consolidate noise.
  4. Hand to an LLM via ``summarize_fn`` — caller wires the call.
  5. Write the summary back as a single memory in a ``consolidated``
     namespace; delete originals unless ``keep_originals=True``.

Same injectable-dependency philosophy as the rest of the framework:
``summarize_fn`` is the LLM hook. No LLM SDK is imported here.

``summarize_fn`` contract:

  * Input: ``Sequence[Memory]`` — the rows the caller chose to
    consolidate, in ``store.list()`` order (newest first).
  * Output: a single ``str`` — the consolidated content. The function
    is responsible for prompt, model choice, and any
    truncation / chunking.
  * Sync only — wrap async LLM calls in ``asyncio.run(...)`` at the
    call site.

``dry_run`` materializes the consolidated :class:`Memory` client-side
without writing or deleting. The id is a placeholder minted client-side
and will NOT match what the store would mint on a real ``add()``.

Mirrors ``typescript/src/memory-consolidate.ts``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ._memory import Memory, MemoryFilter, MemoryStore, materialize_memory

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


SummarizeFn = Callable[[Sequence[Memory]], str]


@dataclass(frozen=True)
class ConsolidateResult:
    """Result returned by :func:`consolidate_memories`.

    Attributes:
        candidates_found: Number of memories that matched the
            principal / namespace / importance / age filters.
        consolidated_memory: The materialized consolidated :class:`Memory`,
            or ``None`` when ``candidates_found < min_memories_for_consolidation``.
        deleted_ids: Ids of original memories deleted after the
            consolidation write. Empty when ``keep_originals=True`` or
            ``dry_run=True``.
    """

    candidates_found: int
    consolidated_memory: Memory | None
    deleted_ids: tuple[str, ...]


def _parse_iso_seconds(ts: str) -> float | None:
    """Parse the ISO-8601 timestamp ``Memory`` writes and return epoch seconds.

    The format ``materialize_memory`` writes is
    ``YYYY-MM-DDTHH:MM:SS.sssZ``. We accept that plus Python's standard
    ``fromisoformat`` syntax. Returns ``None`` when unparseable.
    """
    try:
        # Strip trailing Z and parse as UTC.
        from datetime import datetime, timezone

        clean = ts[:-1] if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Consolidation
# ---------------------------------------------------------------------------


def consolidate_memories(
    *,
    store: MemoryStore,
    principal_id: str,
    summarize_fn: SummarizeFn,
    namespace: str | None = None,
    max_age_seconds: float | None = None,
    min_importance: float | None = None,
    keep_originals: bool = False,
    consolidated_namespace: str = "consolidated",
    consolidated_tags: Sequence[str] = ("consolidated",),
    consolidated_importance: float = 0.7,
    min_memories_for_consolidation: int = 5,
    dry_run: bool = False,
) -> ConsolidateResult:
    """Consolidate a principal's older memories into a single summary row."""
    raw = store.list(
        MemoryFilter(
            principal_id=principal_id,
            namespace=namespace,
            min_importance=min_importance,
            limit=10_000,
        )
    )

    if max_age_seconds is not None:
        cutoff = time.time() - max_age_seconds
        candidates: list[Memory] = [
            m
            for m in raw
            if (parsed := _parse_iso_seconds(m.updated_at)) is not None
            and parsed < cutoff
        ]
    else:
        candidates = list(raw)

    candidates_found = len(candidates)

    if candidates_found < min_memories_for_consolidation:
        return ConsolidateResult(
            candidates_found=candidates_found,
            consolidated_memory=None,
            deleted_ids=(),
        )

    content = summarize_fn(candidates)
    source_ids = [m.id for m in candidates]

    input_data: dict[str, Any] = {
        "principal_id": principal_id,
        "content": content,
        "namespace": consolidated_namespace,
        "tags": tuple(consolidated_tags),
        "importance": consolidated_importance,
        "metadata": {
            "consolidated_from": source_ids,
            "count": len(candidates),
        },
    }

    if dry_run:
        return ConsolidateResult(
            candidates_found=candidates_found,
            consolidated_memory=materialize_memory(input_data),
            deleted_ids=(),
        )

    consolidated_memory = store.add(input_data)

    deleted: list[str] = []
    if not keep_originals:
        for mid in source_ids:
            if store.delete(mid):
                deleted.append(mid)

    return ConsolidateResult(
        candidates_found=candidates_found,
        consolidated_memory=consolidated_memory,
        deleted_ids=tuple(deleted),
    )


__all__ = [
    "ConsolidateResult",
    "SummarizeFn",
    "consolidate_memories",
]
