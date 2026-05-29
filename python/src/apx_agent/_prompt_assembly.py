"""prompt-assembly — pure helpers for stitching memory + example recall
output into a system-prompt-ready string.

No I/O of its own; takes the stores as arguments and calls their recall
paths. Returns markdown-formatted strings the caller can splice
directly into a system message. Empty stores → empty strings, so
callers can unconditionally concat without worrying about stray headers
or whitespace.

Mirrors ``typescript/src/prompt-assembly.ts``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._example import ExampleStore, FindSimilarOptions
from ._memory import MemoryStore, RecallOptions

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


DEFAULT_MEMORY_HEADER = "### Relevant memories"
"""Default markdown header for the memory block."""


DEFAULT_EXAMPLE_HEADER = "### Relevant examples"
"""Default markdown header for the examples block."""


# ---------------------------------------------------------------------------
# Internal coercion
# ---------------------------------------------------------------------------


def _coerce_recall_options(opts: RecallOptions | Mapping[str, Any]) -> RecallOptions:
    """Accept either a :class:`RecallOptions` or a plain dict.

    Dicts are coerced via the dataclass constructor so callers can use
    the more Pythonic kwarg-style invocation. ``tags`` (if present as a
    list) is normalized to a tuple to satisfy the frozen dataclass.
    """
    if isinstance(opts, RecallOptions):
        return opts
    kwargs: dict[str, Any] = dict(opts)
    tags = kwargs.get("tags")
    if tags is not None and not isinstance(tags, tuple):
        kwargs["tags"] = tuple(tags)
    return RecallOptions(**kwargs)


def _coerce_find_options(
    opts: FindSimilarOptions | Mapping[str, Any],
) -> FindSimilarOptions:
    """Accept either a :class:`FindSimilarOptions` or a plain dict."""
    if isinstance(opts, FindSimilarOptions):
        return opts
    kwargs: dict[str, Any] = dict(opts)
    tags = kwargs.get("tags")
    if tags is not None and not isinstance(tags, tuple):
        kwargs["tags"] = tuple(tags)
    return FindSimilarOptions(**kwargs)


# ---------------------------------------------------------------------------
# Memory context
# ---------------------------------------------------------------------------


def assemble_memory_context(
    store: MemoryStore,
    opts: RecallOptions | Mapping[str, Any],
    header: str = DEFAULT_MEMORY_HEADER,
) -> str:
    """Render the top-k memory recall as a markdown block.

    Calls ``store.recall(opts)``, formats each result as
    ``"- {content}"``, and prepends ``header``. Returns the empty
    string when no memories match — callers can unconditionally concat
    without stray headers.

    Args:
        store: A :class:`MemoryStore` to query.
        opts: Either a :class:`RecallOptions` instance or a dict that
            can be unpacked into one.
        header: Markdown header prepended to the block when non-empty.
            Defaults to ``"### Relevant memories"``.

    Returns:
        A markdown block (header + bullets) or ``""`` when no memories.
    """
    recall_opts = _coerce_recall_options(opts)
    results = store.recall(recall_opts)
    if len(results) == 0:
        return ""
    lines = [f"- {r.memory.content}" for r in results]
    return header + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Example context
# ---------------------------------------------------------------------------


def assemble_example_context(
    store: ExampleStore,
    opts: FindSimilarOptions | Mapping[str, Any],
    header: str = DEFAULT_EXAMPLE_HEADER,
) -> str:
    """Render the top-k example retrieval as a markdown block.

    Calls ``store.find_similar(opts)`` and formats each result as a
    ``**Q:** input\\n**A:** output`` block, joined with ``\\n---\\n``.
    Returns the empty string when no examples match.

    Args:
        store: An :class:`ExampleStore` to query.
        opts: Either a :class:`FindSimilarOptions` instance or a dict
            that can be unpacked into one.
        header: Markdown header prepended to the block when non-empty.
            Defaults to ``"### Relevant examples"``.

    Returns:
        A markdown block (header + Q/A blocks) or ``""`` when no
        examples.
    """
    find_opts = _coerce_find_options(opts)
    results = store.find_similar(find_opts)
    if len(results) == 0:
        return ""
    blocks = [
        f"**Q:** {r.example.input}\n**A:** {r.example.output}" for r in results
    ]
    return header + "\n" + "\n---\n".join(blocks)


# ---------------------------------------------------------------------------
# Combined
# ---------------------------------------------------------------------------


def assemble_context(
    memory: Mapping[str, Any] | None = None,
    examples: Mapping[str, Any] | None = None,
    separator: str = "\n\n",
) -> str:
    """Combine memory + example blocks into one system-prompt-ready string.

    Each ``memory`` / ``examples`` arg is a dict with keys ``store``,
    ``opts``, and optionally ``header``. Empty blocks are dropped, so
    the separator never appears next to a missing block.

    Args:
        memory: Optional ``{"store": MemoryStore, "opts": ...,
            "header": str?}`` mapping describing the memory block.
        examples: Optional ``{"store": ExampleStore, "opts": ...,
            "header": str?}`` mapping describing the example block.
        separator: Joiner between two non-empty blocks. Defaults to
            ``"\\n\\n"``.

    Returns:
        Concatenated markdown context, or ``""`` when neither block
        produced output.
    """
    blocks: list[str] = []

    if memory is not None:
        block = assemble_memory_context(
            memory["store"],
            memory["opts"],
            memory.get("header", DEFAULT_MEMORY_HEADER),
        )
        if block:
            blocks.append(block)

    if examples is not None:
        block = assemble_example_context(
            examples["store"],
            examples["opts"],
            examples.get("header", DEFAULT_EXAMPLE_HEADER),
        )
        if block:
            blocks.append(block)

    return separator.join(blocks)


# ---------------------------------------------------------------------------
# Persona overlay
# ---------------------------------------------------------------------------


def compose_instructions(*, base: str | None, overlay: str | None) -> str:
    """Compose a persona ``overlay`` above a grounded ``base`` system prompt.

    Used when a template produced grounded instructions (``base``) AND the
    config envelope supplied persona instructions (``overlay``). The overlay
    goes first so tone/role framing precedes the concrete grounding. When only
    one side is non-empty, that side is returned verbatim (fill semantics).
    """
    base_s = (base or "").strip()
    overlay_s = (overlay or "").strip()
    if base_s and overlay_s:
        return f"{overlay_s}\n\n---\n\n{base_s}"
    return overlay_s or base_s


__all__ = [
    "DEFAULT_EXAMPLE_HEADER",
    "DEFAULT_MEMORY_HEADER",
    "assemble_context",
    "assemble_example_context",
    "assemble_memory_context",
    "compose_instructions",
]
