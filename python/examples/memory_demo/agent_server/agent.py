"""memory_demo agent — Apps-target shape.

This is the Databricks-Apps version of the demo. It's a one-to-one
translation of ``examples/memory_demo/app.py``:

  * Store seeding, ``make_memory_tools`` wiring, and the
    ``assemble_context`` system-prompt build all stay at module level
    (executed once when Apps imports the module).
  * The ``Agent`` definition is unchanged.
  * Only the request entry points are new: ``compile_to_responses_agent``
    produces a ``CompiledResponsesAgent`` and we expose ``invoke``/
    ``stream`` functions registered with ``mlflow.genai.agent_server``.

The companion ``app.py`` still runs in-process — same module, no infra
— for ad-hoc smoke testing without a workspace round-trip.
"""

from __future__ import annotations

import os

from mlflow.genai.agent_server import invoke, stream

from apx_agent import (
    Agent,
    InMemoryExampleStore,
    InMemoryMemoryStore,
    assemble_context,
    make_memory_tools,
)

# TODO(apps-target): ``compile_to_responses_agent`` is being added by the
# parallel work tracked at task #76 (Add Apps target). Expected signature:
#
#   compile_to_responses_agent(agent: BaseAgent, *, model: str) -> CompiledResponsesAgent
#
# where ``CompiledResponsesAgent`` exposes ``.invoke(request: ResponsesAgentRequest)
# -> ResponsesAgentResponse`` and ``.stream(request: ResponsesAgentRequest) ->
# Iterator[ResponsesAgentStreamEvent]``.
#
# Once that lands, the import below resolves. Until then this module will
# raise ImportError on import — that's the expected pre-merge state.
from apx_agent import compile_to_responses_agent  # noqa: F401 (resolved post-merge)

# ---------------------------------------------------------------------------
# 1. Wire stores at module load. Apps loads this module once per worker, so
# store seeding runs once per replica.
# ---------------------------------------------------------------------------


PRINCIPAL_ID = "alice"
AGENT_ID = "travel_concierge"
MODEL = os.environ.get("APX_MODEL", "databricks-claude-sonnet-4-6")


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


# Pre-seed a couple of few-shot examples too.
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
# 3. Memory tools — `recall` / `remember` / `forget` bound to the store.
# ---------------------------------------------------------------------------


memory_tools = make_memory_tools(
    store=memory_store,
    default_principal_id=PRINCIPAL_ID,
    namespace_default="profile",
)


# ---------------------------------------------------------------------------
# 4. System prompt — pull relevant memories + few-shot examples and render
# them as markdown. In the in-process demo this resolves once; in the Apps
# deploy this also resolves once at module load. A session-aware deployment
# would move this into a per-turn callback, but memory_demo stays simple.
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


# ---------------------------------------------------------------------------
# 5. Agent definition — identical to ``app.py``.
# ---------------------------------------------------------------------------


agent = Agent(
    name=AGENT_ID,
    instructions=SYSTEM_PROMPT,
    tools=memory_tools,
)


# ---------------------------------------------------------------------------
# 6. Compile to the ResponsesAgent contract and register Apps entry points.
# ---------------------------------------------------------------------------


_invoke_fn, _stream_fn = compile_to_responses_agent(agent, model=MODEL)


@invoke()
def non_streaming(request):
    """Synchronous request handler — Databricks Apps /invocations target."""
    return _invoke_fn(request)


@stream()
def streaming(request):
    """Streaming request handler — yields ResponsesAgentStreamEvent chunks."""
    yield from _stream_fn(request)
