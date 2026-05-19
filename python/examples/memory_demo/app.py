"""memory_demo — wire memory + examples into one agent end-to-end.

This is the canonical 'how do I bolt memory and few-shot examples onto an
apx-agent' worked example.

What this file demonstrates:

  1. Wire an :class:`InMemoryMemoryStore` and :class:`InMemoryExampleStore`
     at module load. (Lakebase / Delta variants drop in cleanly when
     persistence matters.)
  2. Pre-seed memories + few-shot examples for a fake principal.
  3. Wire ``recall`` / ``remember`` / ``forget`` tools via
     :func:`make_memory_tools` so the LLM can call them mid-turn.
  4. Build the system prompt via :func:`assemble_context`, which pulls
     relevant memories and few-shot examples from the stores and formats
     them as markdown. In a session-aware deployment this runs per turn
     from a callback; in this demo it runs once at module load.
  5. Print the round-trip end-to-end (no LLM call required — store recall
     and the remember tool both run pure-Python, so the demo is reproducible
     without a serving endpoint).

Run::

    cd python
    uv run python -m examples.memory_demo.app
"""

from __future__ import annotations

from apx_agent import (
    Agent,
    InMemoryExampleStore,
    InMemoryMemoryStore,
    assemble_context,
    make_memory_tools,
)

# ---------------------------------------------------------------------------
# 1. Wire stores at module load. In production these are typically Lakebase-
# backed; the InMemory variants keep the demo runnable without infra.
# ---------------------------------------------------------------------------


PRINCIPAL_ID = "alice"
AGENT_ID = "travel_concierge"

memory_store = InMemoryMemoryStore()
example_store = InMemoryExampleStore()


# 2. Pre-seed five memories for the demo principal.
_seed_memories = [
    {"content": "alice prefers window seats on flights longer than 4 hours",
     "tags": ("preference", "seating")},
    {"content": "alice is vegetarian and avoids dairy",
     "tags": ("preference", "diet")},
    {"content": "alice has TSA PreCheck — KTN ending 4421",
     "tags": ("profile",)},
    {"content": "alice flew BOS->SFO on 2026-01-12 (UA 526)",
     "tags": ("episodic",)},
    {"content": "alice's emergency contact is partner Jess at +1-555-0144",
     "tags": ("profile", "contact")},
]
for seed in _seed_memories:
    memory_store.add({
        "principal_id": PRINCIPAL_ID,
        "namespace": "profile",
        "content": seed["content"],
        "tags": list(seed["tags"]),
    })


# Pre-seed a couple of few-shot examples too — the shape demonstration.
for ex in [
    {"input": "what's my next flight?",
     "output": "Pulling up your itinerary now.",
     "intent": "lookup"},
    {"input": "switch me to a window seat",
     "output": "Done — moved you to 14A.",
     "intent": "modify"},
    {"input": "is my meal preference saved?",
     "output": "Yes, vegetarian, no dairy is on file.",
     "intent": "lookup"},
]:
    example_store.add({
        "agent_id": AGENT_ID,
        "input": ex["input"],
        "output": ex["output"],
        "intent": ex["intent"],
    })


# ---------------------------------------------------------------------------
# 3. Build the agent's tool set via the framework's make_memory_tools helper.
# These are @tool-decorated callables bound to the store; the LLM can invoke
# them mid-turn to recall facts or persist new ones.
# ---------------------------------------------------------------------------


memory_tools = make_memory_tools(
    store=memory_store,
    default_principal_id=PRINCIPAL_ID,
    namespace_default="profile",
)


# ---------------------------------------------------------------------------
# 4. Build the agent's system prompt via assemble_context — pulls relevant
# memories + few-shot examples from the stores and renders them as markdown
# blocks. In a session-aware deployment this runs per turn from a callback;
# here we resolve once for the demo prompt.
# ---------------------------------------------------------------------------


DEMO_QUERY = "what seat do I usually pick?"
CONTEXT_BLOCK = assemble_context(
    memory={
        "store": memory_store,
        "opts": {
            "principal_id": PRINCIPAL_ID,
            "query": DEMO_QUERY,
            "k": 3,
        },
        "header": "### What we know about the user",
    },
    examples={
        "store": example_store,
        "opts": {
            "agent_id": AGENT_ID,
            "query": DEMO_QUERY,
            "k": 2,
        },
        "header": "### Example interactions for this agent",
    },
)


SYSTEM_PROMPT = (
    CONTEXT_BLOCK
    + "\n\n"
    + "You are a helpful travel concierge for the named user. Use the recall "
    + "tool to look up additional facts mid-conversation. Use remember to "
    + "persist new facts the user shares."
)


agent = Agent(
    name=AGENT_ID,
    instructions=SYSTEM_PROMPT,
    tools=memory_tools,
)


# ---------------------------------------------------------------------------
# 5. Reproducible demo path — no LLM endpoint required. Exercises the recall
# tool and the after-response 'remember' path directly so the user can see
# the full round-trip in stdout.
# ---------------------------------------------------------------------------


def _find_tool(name: str):
    """Locate a tool by its decorated name in the `memory_tools` list."""
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
