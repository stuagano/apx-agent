"""memory_demo — in-process demo runner.

This file is the local-only demo path — it imports the same ``agent``
that the Databricks Apps target deploys (from ``agent.py``) and runs a
reproducible round-trip in stdout (no LLM endpoint required).

The Apps deploy path uses the same ``agent`` symbol, wrapped by
``agent_server/start_server.py`` for the MLflow GenAI runtime.

Run::

    cd python
    uv run python -m examples.memory_demo.app
"""

from __future__ import annotations

from examples.memory_demo.agent import (
    AGENT_ID,
    DEMO_QUERY,
    PRINCIPAL_ID,
    SYSTEM_PROMPT,
    memory_store,
    memory_tools,
)


def _find_tool(name: str):
    """Locate a tool by its decorated name in the ``memory_tools`` list."""
    for fn in memory_tools:
        if getattr(fn, "__name__", "") == name:
            return fn
    raise LookupError(f"tool {name!r} not in memory_tools")


def _demo() -> None:
    """Print the recall block, run the recall tool, and persist a new memory."""
    recall_fn = _find_tool("recall")
    remember_fn = _find_tool("remember")

    print("=" * 72)
    print("memory_demo — apx-agent memory + examples worked example")
    print("=" * 72)
    print()
    print("PRINCIPAL  :", PRINCIPAL_ID)
    print("AGENT      :", AGENT_ID)
    print("DEMO QUERY :", DEMO_QUERY)
    print()
    print("-" * 72)
    print("System prompt assembled at module load (memories + few-shot examples)")
    print("-" * 72)
    print(SYSTEM_PROMPT)
    print()
    print("-" * 72)
    print("Mid-turn `recall` tool call (what the agent would invoke)")
    print("-" * 72)
    print(recall_fn("what does alice prefer to eat?"))
    print()
    print("-" * 72)
    print("After-response `remember` tool call (new fact from the user)")
    print("-" * 72)
    print(remember_fn(
        "alice booked a hotel at SFO Marriott Marquis for 2026-06-10",
    ))
    print()
    print("done.")


def _safe_count() -> int:
    """Best-effort count of stored memories (works even if the store backend changes)."""
    from apx_agent._memory import MemoryFilter
    return len(memory_store.list(MemoryFilter(principal_id=PRINCIPAL_ID, limit=10_000)))


if __name__ == "__main__":
    _demo()
    print(f"final stored memory count for {PRINCIPAL_ID}: {_safe_count()}")
