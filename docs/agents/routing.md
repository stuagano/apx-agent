# Routing agents

LLM-driven routing patterns where the model selects the branch or peer to invoke.

| Agent | Purpose |
|-------|---------|
| `RouterAgent` | Conditional routing — pick one branch from a closed set |
| `HandoffAgent` | Peer handoff — transfer the conversation to a specialist mid-session |
| `KeywordRouter` | Keyword-based routing without an LLM call |

For composition patterns (Sequential, Parallel, Loop, agent_tool), see [composition.md](composition.md).

## Router — conditional routing

LLM picks one branch based on the user's input. Use when the route is data-dependent but the branch agents are fixed.

```python
from apx_agent import RouterAgent

router = RouterAgent({
    "billing": billing_agent,
    "technical": tech_agent,
    "data": data_agent,
})
```

### Description-driven routing

Pass agents as a plain list — `RouterAgent` reads `name` and `description` from each agent and uses them as the routing table:

```python
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

`description` is the routing signal the LLM sees — write it like tool documentation.

## Handoff — peer handoff mid-conversation

An agent transfers the conversation to a peer agent. The new agent inherits the conversation state.

```python
from apx_agent import HandoffAgent

triage = HandoffAgent(
    agents={
        "billing_agent": billing_agent,
        "technical_agent": tech_agent,
    },
)
```

### List form

Pass agents as a list; `HandoffAgent` uses each agent's `name` attribute as the key and defaults `start` to the first agent:

```python
triage = HandoffAgent(
    agents=[triage_agent, billing_agent, tech_agent],
    # start defaults to "triage" (first agent's name)
)
```

Each agent's `description` becomes the transfer tool's description visible to the LLM.

## Decision guide

| | Decision shape | Control after |
|---|---|---|
| `RouterAgent` | One — pick a branch from a closed set | Branch returns; routing is done |
| `HandoffAgent` | One — pick a peer, transfer | Conversation moves to peer; original agent is out |
| `agent_tool` | Many — call sub-agents like tools | Parent stays in charge, can call again |

See [composition.md](composition.md) for `agent_tool` details.
