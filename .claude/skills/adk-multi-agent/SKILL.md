---
name: adk-multi-agent
description: Apply Google ADK multi-agent design judgment to apx-agent. Use when deciding whether a task needs one agent or several, how to decompose an agent, or which composition primitive to reach for — SequentialAgent vs ParallelAgent vs LoopAgent vs agent_tool vs RouterAgent vs HandoffAgent — and when building coordinator/orchestrator, generator-critic, or fan-out/gather structures across local or remote (A2A) sub-agents.
---

# ADK multi-agent design

Google ADK's multi-agent guidance, mapped onto apx-agent's declared
composition primitives. This skill is *judgment* — which primitive, and why.
For syntax, follow the links to the canonical docs.

## Start with one agent

Default to a single `Agent`. Split only when a real boundary exists:

- **Distinct tool sets** the LLM keeps confusing.
- **Distinct governance/identity** — a sub-agent that must run under a
  different principal or resource grant.
- **Independent scaling or reuse** — a specialist other agents will call.
- **A closed routing decision** — mutually exclusive branches.

Decomposition is a response to a boundary, not a starting posture. "One agent
per step" is the most common ADK anti-pattern; splitting adds latency, tokens,
and routing failure modes. If no boundary above applies, keep it one agent.

## Pick the primitive

First axis — **who decides the structure?**

- **Fixed at definition time (code decides):** `SequentialAgent` (pipeline,
  each step sees the prior output), `ParallelAgent` (fan-out / gather
  independent work), `LoopAgent` (iterate until `finish_loop()` or
  `max_iterations` — generator-critic / draft-review-revise).
- **Chosen by the LLM at runtime:** `agent_tool`, `RouterAgent`,
  `HandoffAgent`, `KeywordRouter`.

Second axis — **for LLM-chosen, what kind of decision?**

| Shape | Primitive |
|-------|-----------|
| One decision, mutually exclusive branches, parent then done | `RouterAgent` |
| Deterministic keyword match, no LLM call | `KeywordRouter` |
| Full transfer — specialist takes over the conversation | `HandoffAgent` |
| Parent stays in charge, may delegate repeatedly, interleave | `agent_tool` |

Common structures:
- **Coordinator/dispatcher** → `RouterAgent`, or an orchestrator holding
  `agent_tool` sub-agents when it must stay in charge.
- **Generator-critic** → `LoopAgent`.
- **Fan-out/gather** → `ParallelAgent`.

## `name` and `description` are the routing contract

For every LLM-chosen primitive, the sub-agent's `name` and `description` are
the *only* thing the parent LLM sees. Write them like tool docs: when to call,
what it does, what it returns. Vague descriptions are the #1 cause of bad
delegation. This is the single highest-leverage best practice in a multi-agent
system.

## Local and remote are the same wrapper

A remote sub-agent is a `RemoteDatabricksAgent` wrapped by the same
`agent_tool`, or a `sub_agents=[url]` entry auto-resolved at startup. In-process and Model Serving deployments pass the caller's identity
automatically — declared, not wired. A2A app-to-app crosses a service-principal
boundary: the caller's SP needs CAN_USE on the callee, and the callee's internal
model calls run under the callee's own SP (not the caller's token); for
turnkey user-scoped passthrough across apps, prefer Model Serving deployment.

## See also

- Tool `name`/`description` and structured returns: `adk-tool-design`.
- Guardrails on delegated calls (`before_tool`, allowlists): `adk-safety-callbacks`.

## Read next

- [Agent composition](../../../docs/agents/composition.md) — `SequentialAgent`,
  `ParallelAgent`, `LoopAgent`, `agent_tool`, remote sub-agents, `sub_agents=[...]`.
- [Routing agents](../../../docs/agents/routing.md) — `RouterAgent`,
  `HandoffAgent`, `KeywordRouter`.
- [Multi-agent overview](../../../docs/multi-agent/overview.md) and
  [A2A](../../../docs/multi-agent/a2a.md) — cross-app agents and identity passthrough.
