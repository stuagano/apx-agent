# Routing agents

LLM-driven routing patterns where the model selects the branch or peer to invoke.

## Core ideas

- **`RouterAgent`** makes one routing decision per invocation: the LLM picks one branch from a closed set, that branch runs, and routing is done. The parent agent is not involved further.
- **`HandoffAgent`** is a peer-transfer pattern: the LLM picks a specialist from a closed set and transfers the whole conversation to it. The originating agent exits; the specialist takes over from that point forward.
- **`KeywordRouter`** routes without an LLM call — use it when the routing rule is a keyword match and you want deterministic, zero-latency routing.
- **`agent_tool`** (see [composition.md](composition.md)) is the right choice when the parent needs to stay in charge and may delegate multiple times.

| Agent | Purpose |
|-------|---------|
| `RouterAgent` | Conditional routing — pick one branch from a closed set |
| `HandoffAgent` | Peer handoff — transfer the conversation to a specialist mid-session |
| `KeywordRouter` | Keyword-based routing without an LLM call |

## RouterAgent — conditional routing

The LLM picks one branch based on the user's input. Use when the route is data-dependent but the branch agents are fixed.

Pass agents as a list — `RouterAgent` reads `name` and `description` from each agent and uses them as the routing table:

```python
from apx_agent import RouterAgent, Agent

billing_agent = Agent(
    name="billing",
    description="Handles billing inquiries, invoice lookups, and payment issues.",
    tools=[...],
)
tech_agent = Agent(
    name="technical",
    description="Handles technical support, error messages, and configuration issues.",
    tools=[...],
)

router = RouterAgent([billing_agent, tech_agent])
```

`description` is the routing signal the LLM sees — write it like tool documentation: what topics this agent handles, when to route here.

### Explicit tuple form

Pass `(name, description, agent)` triples to set routing metadata separately from the agent definition. Use this when you want the router's view of each branch to differ from the agent's own `name`/`description`.

```python
router = RouterAgent([
    ("billing", "Handles billing inquiries, invoice lookups, and payment issues.", billing_agent),
    ("technical", "Handles technical support, error messages, and configuration issues.", tech_agent),
])
```

## HandoffAgent — peer handoff mid-conversation

An agent transfers the conversation to a peer. The new agent inherits the full conversation state. Use when a triage agent should hand off to a specialist and step aside.

```python
from apx_agent import HandoffAgent, Agent

triage = HandoffAgent(
    agents={
        "billing_agent": billing_agent,
        "technical_agent": tech_agent,
    },
)
```

### List form

Pass agents as a list; `HandoffAgent` uses each agent's `name` attribute as the key and defaults `start` to the first agent.

```python
triage = HandoffAgent(
    agents=[triage_agent, billing_agent, tech_agent],
    # start defaults to "triage" (first agent's name)
)
```

Each agent's `description` becomes the transfer tool's description visible to the LLM — same rule as `RouterAgent`: write it like documentation.

## KeywordRouter — keyword-based routing

Routes without an LLM call. Use when the routing rule is a keyword match and you want deterministic, zero-latency routing.

Pass `(name, agent, [keywords])` triples and a required `default` agent. The first branch whose keywords appear in the latest user message wins; if none match, the `default` agent handles it.

```python
from apx_agent import KeywordRouter, Agent

router = KeywordRouter(
    branches=[
        ("billing", billing_agent, ["invoice", "payment", "bill"]),
        ("technical", tech_agent, ["error", "config", "setup"]),
    ],
    default=general_agent,
)
```

## Decision guide

| | Decision shape | Control after |
|---|---|---|
| `RouterAgent` | One — LLM picks a branch from a closed set | Branch returns; routing is done |
| `HandoffAgent` | One — LLM picks a peer, transfer | Conversation moves to peer; original agent exits |
| `KeywordRouter` | One — keyword match, no LLM call | Branch returns; routing is done |
| `agent_tool` | Many — LLM calls sub-agents like tools | Parent stays in charge; can call again |

See [composition.md](composition.md) for `agent_tool` and composition patterns.
