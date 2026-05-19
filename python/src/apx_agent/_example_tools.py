"""example-tools — agent-facing tools wrapping an :class:`ExampleStore`.

Builds three :func:`apx_agent.tool`-decorated callables
(``find_examples`` / ``save_example`` / ``remove_example``) so an LLM
can read and write few-shot examples through standard tool-calling.

``agent_id`` is resolved per-call: the optional ``agent_id_resolver``
runs first (typically reading request-scoped context), falling back to
``default_agent_id``. When neither resolves, the tool degrades
gracefully by returning a no-agent string rather than raising.

Mirrors ``typescript/src/example-tools.ts``. The Python output format
for ``find_examples`` uses the same ``**Q:** input\\n**A:** output``
block format the prompt-assembly helper uses, so the two surfaces
present identical Q/A formatting to the LLM.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

from ._example import ExampleResult, ExampleStore, FindSimilarOptions
from ._tool import tool

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


ExampleToolName = Literal["find_examples", "save_example", "remove_example"]
"""Names of the tools this factory can mint."""


NO_AGENT = "No agent_id available; cannot look up examples."
"""Returned by find_examples / save_example when no agent_id resolves.

Matches the TypeScript constant verbatim so agents that key off this
sentinel see the same string in both runtimes.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_example_results(results: Sequence[ExampleResult]) -> str:
    """Render examples as ``**Q:** ... \\n**A:** ...`` blocks.

    Blocks are joined with ``\\n---\\n``. Empty input returns the
    ``"No examples found."`` sentinel.
    """
    if len(results) == 0:
        return "No examples found."
    blocks: list[str] = []
    for r in results:
        blocks.append(f"**Q:** {r.example.input}\n**A:** {r.example.output}")
    return "\n---\n".join(blocks)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_example_tools(
    *,
    store: ExampleStore,
    agent_id_resolver: Callable[[], str | None] | None = None,
    default_agent_id: str | None = None,
    intent_default: str | None = None,
    tool_prefix: str = "",
    include: list[ExampleToolName] | None = None,
) -> list[Any]:
    """Build LLM-facing example tools that close over ``store``.

    Returns a list of :func:`apx_agent.tool`-decorated callables. The
    ``agent_id`` is resolved per-call: ``agent_id_resolver()`` first
    (return ``None`` to fall through), then ``default_agent_id``. When
    neither yields a value, ``find_examples`` / ``save_example`` return
    :data:`NO_AGENT` instead of raising.

    ``remove_example`` is agent-agnostic — deletion is keyed by example
    id, not by agent.

    Args:
        store: The :class:`ExampleStore` instance to wrap. Required.
        agent_id_resolver: Optional callable invoked at tool-call time
            to fetch the current ``agent_id``. Return ``None`` to fall
            through to ``default_agent_id``.
        default_agent_id: Optional fallback ``agent_id`` used when the
            resolver returns ``None`` (or is absent).
        intent_default: Optional intent passed through when the caller
            omits the ``intent`` argument. ``None`` (the default) leaves
            the store filter unset.
        tool_prefix: Optional prefix prepended to every minted tool name
            (e.g. ``"ex_"`` produces ``"ex_find_examples"``).
        include: Optional subset of tool names to build. Defaults to
            all three.

    Returns:
        A list of decorated callables, in the order requested by
        ``include`` (or
        ``[find_examples, save_example, remove_example]`` by default).
    """
    requested: list[ExampleToolName] = (
        list(include)
        if include is not None
        else ["find_examples", "save_example", "remove_example"]
    )

    def _resolve_agent() -> str | None:
        """Run resolver-then-default lookup — once per tool call."""
        if agent_id_resolver is not None:
            from_resolver = agent_id_resolver()
            if from_resolver:
                return from_resolver
        return default_agent_id

    tools: list[Any] = []

    for name in requested:
        if name == "find_examples":

            @tool(name=f"{tool_prefix}find_examples")
            def find_examples(
                query: str,
                k: int = 5,
                intent: str | None = None,
                tags: list[str] | None = None,
            ) -> str:
                """Find few-shot examples similar to ``query``.

                Returns the top-k matches formatted as
                ``**Q:** ... \\n**A:** ...`` blocks joined with
                ``\\n---\\n``. Returns ``"No examples found."`` when
                nothing matches.
                """
                agent_id = _resolve_agent()
                if not agent_id:
                    return NO_AGENT
                results = store.find_similar(
                    FindSimilarOptions(
                        agent_id=agent_id,
                        query=query,
                        k=k,
                        intent=intent if intent is not None else intent_default,
                        tags=tuple(tags) if tags is not None else None,
                    )
                )
                return _format_example_results(results)

            tools.append(find_examples)

        elif name == "save_example":

            @tool(name=f"{tool_prefix}save_example")
            def save_example(
                input: str,
                output: str,
                intent: str | None = None,
                score: float | None = None,
                tags: list[str] | None = None,
            ) -> str:
                """Save a few-shot example for later retrieval.

                Persists ``input``/``output`` under the current
                ``agent_id`` and returns the new example id.
                """
                if score is not None and not (0.0 <= score <= 1.0):
                    raise ValueError(
                        f"score must be in [0, 1] when provided; got {score}"
                    )
                agent_id = _resolve_agent()
                if not agent_id:
                    return NO_AGENT
                example = store.add(
                    {
                        "agent_id": agent_id,
                        "input": input,
                        "output": output,
                        "intent": intent if intent is not None else intent_default,
                        "score": score,
                        "tags": tuple(tags) if tags is not None else (),
                    }
                )
                return f"Saved example {example.id}."

            tools.append(save_example)

        elif name == "remove_example":

            @tool(name=f"{tool_prefix}remove_example")
            def remove_example(id: str) -> str:
                """Delete a few-shot example by id.

                Returns ``"Removed example {id}."`` on hit or
                ``"No example found with id {id}."`` on miss. Agent-
                agnostic — operates by id only.
                """
                deleted = store.delete(id)
                if deleted:
                    return f"Removed example {id}."
                return f"No example found with id {id}."

            tools.append(remove_example)

        else:  # pragma: no cover - guarded by Literal type
            raise ValueError(f"unknown example tool name: {name!r}")

    return tools


__all__ = [
    "ExampleToolName",
    "NO_AGENT",
    "make_example_tools",
]
