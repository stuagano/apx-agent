# Multi-agent systems

An APX application is one declared agent graph. Capability leaves can execute
in the application process or behind another application's A2A boundary while
the graph keeps the same logical names and control structure.

This is the same APX model used for a single agent: declare the capabilities,
compose the root, and attach tools, policies, resources, memory, sessions,
callbacks, and templates where they belong. Splitting a leaf into another app
changes its deployment boundary, not the graph abstraction.

## Capabilities and control are separate

Capability leaves own instructions and work:

- `Agent` / `LlmAgent` for general model-and-tool reasoning
- `DataAgent` for governed Unity Catalog data work
- `CoworkerAgent` for work that joins two landed source systems

Graph-control primitives own execution shape:

- `SequentialAgent` for fixed order
- `ParallelAgent` for concurrent fan-out and gather
- `RouterAgent` for a model-selected branch
- `KeywordRouter` for a deterministic keyword-selected branch
- `LoopAgent` for bounded local repetition
- `HandoffAgent` for local conversational transfer
- `agent_tool` when a parent model should decide whether and how often to
  delegate while retaining control

See [Agent composition](../agents/composition.md) for the API and examples.

## Pick the deployment boundary independently

| Edge | Same application | Separate application |
|---|---|---|
| Declared graph edge | Compose the logical leaf directly | Bind the named logical leaf under `[tool.apx.agent.bindings]` |
| Model-directed delegation | `agent_tool(local_leaf)` | Existing URL-based sub-agent declarations remain available for compatibility |

Keep leaves together when they version, scale, and operate together. Put a
leaf behind A2A when it has independent consumers, lifecycle, scaling, or a
distinct governed resource boundary.

Named bindings are the default deterministic cross-app path:

```toml
[tool.apx.agent]
experiment = "/Shared/research-assistant"

[tool.apx.agent.bindings]
pricing = "$PRICING_APP_URL"
```

```python
from apx_agent import Agent, RouterAgent, SequentialAgent

data = Agent(name="data", description="Looks up governed account facts.")
pricing = Agent(name="pricing", description="Produces an approved price.")
review = SequentialAgent([data, pricing], name="review")
root = RouterAgent(agents=[data, review])
```

APX resolves the binding at application startup. The environment reference,
card location, and transport implementation do not enter generated customer
code or topology labels. Operators and users continue to see `pricing`.

The binding must match exactly one named leaf and resolve to a valid HTTP(S)
A2A card location. Missing, ambiguous, blank, or malformed bindings fail
startup. This makes a deployment mistake explicit rather than silently running
the wrong leaf.

## One root, three ingress protocols

The finalized root is shared by:

- MLflow ChatAgent `POST /invocations`
- MLflow ResponsesAgent `POST /responses`
- A2A `message/send` on `POST /`

Every ingress therefore sees the same router, sequence, tools, policies, and
state configuration. A direct branch is a normal terminal graph path. In the
example, a direct `data` answer makes no remote call; a request routed through
`review` runs the local step and then invokes the bound `pricing` leaf.

APX does not create a shadow workflow for A2A. The outbound call is the runtime
implementation of the selected logical leaf.

## Supported remote-bound control shapes

A named remote-bound leaf can participate in:

- `SequentialAgent`
- `ParallelAgent`
- `RouterAgent`
- `KeywordRouter`

Remote-bound `LoopAgent` and `HandoffAgent` control positions are deliberately
unsupported. Their loop-completion and conversation-transfer semantics require
an A2A control protocol that the current surface does not define, so APX rejects
those declarations instead of approximating them.

## Model-directed delegation

`agent_tool` is different from a fixed or routed graph edge. It turns an agent
into a typed tool; the parent model decides when to call it and can call it
again in the same turn. The parent receives the result and remains in charge.

Use it when delegation is discretionary. Use `SequentialAgent`,
`ParallelAgent`, `RouterAgent`, or `KeywordRouter` when the graph itself should
express the edge.

## Identity and governance across A2A

The caller authenticates to the peer application, and the calling user's OBO
identity is forwarded for user-scoped tool and data access. The peer
application's own model calls use that application's service identity. This
keeps user-governed data access and application-scoped model access explicit at
each hop. See [A2A discovery and app-to-app auth](a2a.md).

Resource declarations and service policies stay attached to the leaf that owns
the operation. Moving that leaf behind A2A does not move its grants or policy
responsibility to the caller.

## Distributed tracing

APX propagates MLflow trace context across the A2A call and continues it before
the peer's request and graph spans begin. To persist the entire cross-app span
tree as one trace in an independently deployed system, every participating app
must write to the same governed MLflow experiment or trace location.

The destination is deployment configuration, not caller input. APX does not
accept an experiment-selection request header and does not extend the A2A card
protocol for tracing. See [Tracing](../running/tracing.md#distributed-tracing-across-apps).

## Handoffs

A local `HandoffAgent` exposes transfer tools derived from each peer's logical
name and description. Once selected, the conversation moves to that peer and
the previous agent exits. Use `RouterAgent` when a branch should return and the
routing decision is complete; use `HandoffAgent` when control itself moves.

Remote-bound handoffs are not supported by the current A2A control surface.

## Durable execution

Local sequential and loop workflows can use the existing workflow engine for
step persistence. `InMemoryEngine` is process-local; `DeltaEngine` persists
workflow state through the SQL Statements API. This durability is separate
from named A2A binding and does not add remote loop or handoff semantics.

## Decision guide

| Goal | Approach |
|---|---|
| Fixed multi-step graph | `SequentialAgent` |
| Concurrent independent work | `ParallelAgent` |
| Model-selected terminal branch | `RouterAgent` |
| Keyword-selected terminal branch | `KeywordRouter` |
| Bounded local refinement | `LoopAgent` |
| Local peer transfer | `HandoffAgent` |
| Discretionary delegation with parent control | `agent_tool` |
| Deterministic leaf in another app | Named `[tool.apx.agent.bindings]` entry |
