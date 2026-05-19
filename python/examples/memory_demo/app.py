"""memory_demo — wire memory + examples into one agent end-to-end.

This is the canonical 'how do I bolt memory and few-shot examples onto an
apx-agent' worked example. Chosen as a new standalone demo (Option B) rather
than retrofitting customer_triage because:

  * customer_triage is a HandoffAgent over four LlmAgents — injecting a
    dynamic-instructions recall block per sub-agent is a fair bit of surgery
    for a demo whose point is the wiring shape, not the routing graph.
  * A small, single-file example keeps the moving parts visible: one store,
    one principal, two tools, one agent. New users grok it in one screen.

What this file demonstrates:

  1. Wire an :class:`InMemoryMemoryStore` and :class:`InMemoryExampleStore`
     at module load. (Lakebase variants drop in cleanly when persistence
     matters.)
  2. Pre-seed memories + few-shot examples for a fake principal.
  3. Expose ``recall`` and ``remember`` tools the agent can call mid-turn.
  4. Show how a system prompt can prepend a memory-recall block — the
     "assemble_context" pattern — by reading from the store at compile time
     for the demo prompt.
  5. Print the round-trip end-to-end (no LLM call required — store recall
     and the remember tool both run pure-Python, so the demo is reproducible
     without a serving endpoint).

Run::

    cd python
    uv run python -m examples.memory_demo.app

Aside on the inline tools: the framework currently doesn't ship a
``make_memory_tools`` helper, so the ``recall`` and ``remember`` tools below
are defined inline against the store. Once that helper lands, the inline
definitions can be replaced with a one-liner — the agent shape is unchanged.
"""

from __future__ import annotations

import json

from apx_agent import (
    Agent,
    InMemoryExampleStore,
    InMemoryMemoryStore,
    tool,
)
from apx_agent._example import FindSimilarOptions
from apx_agent._memory import RecallOptions

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
# 3. Inline recall / remember tools the agent can call.
# Real deployments wire these via a yet-to-land make_memory_tools helper;
# the inline form below is identical in behavior and keeps the demo
# self-contained.
# ---------------------------------------------------------------------------


@tool
def recall(query: str) -> str:
    """Recall memories about the current user matching QUERY.

    Returns a JSON list of {content, score} dicts, top-3.
    """
    results = memory_store.recall(
        RecallOptions(principal_id=PRINCIPAL_ID, query=query, k=3)
    )
    return json.dumps(
        [{"content": r.memory.content, "score": r.score} for r in results],
        indent=2,
    )


@tool
def remember(content: str) -> str:
    """Save a new long-lived memory about the current user.

    Returns the new memory id.
    """
    m = memory_store.add({
        "principal_id": PRINCIPAL_ID,
        "namespace": "profile",
        "content": content,
    })
    return m.id


# ---------------------------------------------------------------------------
# 4. Build the agent's system prompt with a recall block + few-shot examples
# resolved at module load. This is the "assemble_context" pattern: read from
# the stores, format the relevant bits, and prepend them to the static
# instructions. In a session-aware deployment this happens per-turn from a
# callback instead.
# ---------------------------------------------------------------------------


def _assemble_context(query_hint: str) -> str:
    """Build a system-prompt prefix with relevant memories + examples."""
    mems = memory_store.recall(
        RecallOptions(principal_id=PRINCIPAL_ID, query=query_hint, k=3)
    )
    exs = example_store.find_similar(
        FindSimilarOptions(agent_id=AGENT_ID, query=query_hint, k=2)
    )

    parts: list[str] = []
    parts.append("# What we know about the user")
    for r in mems:
        parts.append(f"- {r.memory.content}")
    parts.append("")
    parts.append("# Example interactions for this agent")
    for r in exs:
        parts.append(f"- user: {r.example.input}")
        parts.append(f"  assistant: {r.example.output}")
    return "\n".join(parts)


DEMO_QUERY = "what seat do I usually pick?"
SYSTEM_PROMPT = (
    _assemble_context(DEMO_QUERY)
    + "\n\n"
    + "You are a helpful travel concierge for the named user. Use the recall "
    + "tool to look up additional facts mid-conversation. Use remember to "
    + "persist new facts the user shares."
)


agent = Agent(
    name=AGENT_ID,
    instructions=SYSTEM_PROMPT,
    tools=[recall, remember],
)


# ---------------------------------------------------------------------------
# 5. Reproducible demo path — no LLM endpoint required. Exercises the recall
# tool and the after-response 'remember' path directly so the user can see
# the full round-trip in stdout.
# ---------------------------------------------------------------------------


def _demo() -> None:
    """Print the recall block, run the recall tool, and persist a new memory."""
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
    # Tools are decorated callables; the @tool wrapper is callable as-is.
    recall_out = recall("what does alice prefer to eat?")  # type: ignore[operator]
    print(recall_out)
    print()
    print("-" * 72)
    print("After-response `remember` tool call (new fact from the user)")
    print("-" * 72)
    new_id = remember(  # type: ignore[operator]
        "alice booked a hotel at SFO Marriott Marquis for 2026-06-10",
    )
    print(f"persisted memory id: {new_id}")
    print()
    print("done.")


def _safe_count() -> int:
    """Best-effort count of stored memories (works even if the store backend changes)."""
    from apx_agent._memory import MemoryFilter
    return len(memory_store.list(MemoryFilter(principal_id=PRINCIPAL_ID, limit=10_000)))


if __name__ == "__main__":
    _demo()
    print(f"final stored memory count for {PRINCIPAL_ID}: {_safe_count()}")
