"""memory_demo: travel concierge with persistent memory and few-shot examples.

A single Agent that uses memory tools (recall/remember/forget) bound to an
InMemoryMemoryStore, with relevant memories and few-shot examples assembled
into the system prompt at construction time. Stores, seeds, and prompt
assembly live inline in this module.
"""
from __future__ import annotations

from apx_agent import (
    Agent,
    InMemoryExampleStore,
    InMemoryMemoryStore,
    assemble_context,
    make_memory_tools,
)

# Stores — InMemory for the demo; swap to LakebaseMemoryStore /
# DeltaMemoryStore for shared persistence across App replicas.

PRINCIPAL_ID = "alice"
AGENT_ID = "travel_concierge"

memory_store = InMemoryMemoryStore()
example_store = InMemoryExampleStore()

# Pre-seed memories + few-shot examples for the demo principal.
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


# Memory tools — `recall` / `remember` / `forget` bound to the store.
memory_tools = make_memory_tools(
    store=memory_store,
    default_principal_id=PRINCIPAL_ID,
    namespace_default="profile",
)


# System prompt — assembled with relevant memories + few-shot examples.
DEMO_QUERY = "what seat do I usually pick?"
CONTEXT_BLOCK = assemble_context(
    memory={
        "store": memory_store,
        "opts": {"principal_id": PRINCIPAL_ID, "query": DEMO_QUERY, "k": 3},
        "header": "### What we know about the user",
    },
    examples={
        "store": example_store,
        "opts": {"agent_id": AGENT_ID, "query": DEMO_QUERY, "k": 2},
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
