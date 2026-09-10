# Agent composition

APX is a declarative agent-graph platform. You declare capability leaves, the
control graph that connects them, and the governance and state attached to that
graph. APX compiles the declaration to the served runtime; there is no separate
pipeline product or runner to wire by hand.

## The graph model

Capability leaves do work:

| Leaf | Purpose |
|---|---|
| `Agent` / `LlmAgent` | Model reasoning with typed tools and callbacks. `Agent` is the short public alias for `LlmAgent`. |
| `DataAgent` | An `LlmAgent` grounded in governed Unity Catalog data, with SQL and optional data-service tools. |
| `CoworkerAgent` | A `DataAgent` configured to join two landed source systems around a business key and objective. |

Graph-control primitives decide when leaves run:

| Primitive | Control contract |
|---|---|
| `SequentialAgent` | Run children in a fixed order; each step receives prior context. |
| `ParallelAgent` | Run independent children concurrently and gather their results. |
| `RouterAgent` | Let a model choose one branch from a closed set. |
| `KeywordRouter` | Choose one branch by deterministic, case-insensitive keyword matching. |
| `LoopAgent` | Repeat a local leaf until it calls `finish_loop()` or reaches its cap. |
| `HandoffAgent` | Let one local peer transfer conversational control to another. |

The graph does not replace the declarations attached to it. Tools and their
governed resources, service policies and guardrails, memory and session
backends, model and tool callbacks, and template-built leaves remain part of
the same agent declaration. See [Tools](../tools/overview.md), [Service
policies](../reference/service-policies.md), [Sessions and
memory](../running/sessions-and-memory.md), [Callbacks](../safety/callbacks.md),
and [Configuration](../reference/configuration.md).

## One logical graph across app boundaries

A named binding changes where one logical leaf executes without changing the
Python graph:

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

`pricing` is still a normal named leaf in source and in the visible topology.
At startup, APX resolves the environment reference and binds that one leaf to
the peer's A2A transport internally. Generated customer code remains the
logical graph: it does not import a transport implementation or embed the
environment reference or card URL.

Binding validation fails closed. A binding must name exactly one logical leaf,
the reference must resolve to a valid HTTP(S) A2A card location, and invalid or
ambiguous declarations stop startup rather than silently choosing a target.

The same declared `root` serves all supported ingress protocols:

- `POST /invocations`
- `POST /responses`
- A2A `message/send` on `POST /`

The router's direct `data` answer is an ordinary terminal graph branch: it does
not make an A2A call. If the router selects `review`, the local `data` step runs
before the bound `pricing` leaf. This is one graph with different terminal
paths, not a local graph plus a second transport workflow.

Named remote bindings are supported in `SequentialAgent`, `ParallelAgent`,
`RouterAgent`, and `KeywordRouter` positions. They are not supported in
`LoopAgent` or `HandoffAgent` control positions: remote loop completion and
remote handoff need control semantics that the current A2A surface does not
provide, so APX rejects those declarations.

## SequentialAgent

Each step receives the accumulated conversation, including the previous
step's result. Use it when order is part of the contract.

```python
from apx_agent import Agent, SequentialAgent

research = Agent(
    name="research",
    instructions="Collect the governed facts needed for the request.",
)
review = Agent(
    name="review",
    instructions="Review the facts and produce the final response.",
)
root = SequentialAgent([research, review], name="research_review")
```

Set `output_key` on a producing `Agent` and reference `{key}` in downstream
instructions when a named state value is clearer than conversation context.
Tools that read or write several values can use `Dependencies.State`; see
[custom-tool state sharing](../tools/custom-tools.md#share-state-within-an-invocation).

## ParallelAgent

Use parallel composition for independent work that can run concurrently.

```python
from apx_agent import Agent, ParallelAgent

root = ParallelAgent([
    Agent(name="research", instructions="Collect governed source facts."),
    Agent(name="review", instructions="Check the request against policy."),
])
```

## RouterAgent and KeywordRouter

`RouterAgent` makes one model-directed branch choice. `KeywordRouter` makes a
zero-model-cost substring match and falls back to its declared default. Both
return after the selected branch finishes.

Use `RouterAgent` when the distinction is semantic. Use `KeywordRouter` when
the route is an explicit lexical rule. See [Routing](routing.md).

## LoopAgent and HandoffAgent

`LoopAgent` repeats its local body until `finish_loop()` or `max_iterations`.
Nested iteration budgets multiply, so set every cap deliberately;
`max_iterations=0` is a hard stop, not an unlimited mode.

`HandoffAgent` gives local named peers model-visible transfer tools and moves
control to the selected peer. Use it when the original agent should leave the
conversation rather than receive a sub-agent result.

These are local control protocols. Do not place a remotely bound leaf in their
control positions.

## `agent_tool`: model-directed delegation

`agent_tool` exposes an agent as a typed tool. The parent model decides whether
to call it, what input to pass, and whether to call it again; the parent remains
in control after the result returns.

```python
from apx_agent import Agent, agent_tool

research = Agent(
    name="research",
    description="Collects governed evidence for a question.",
)

router = Agent(
    name="router",
    instructions="Answer directly when possible; delegate evidence gathering when needed.",
    tools=[
        agent_tool(
            research,
            name="ask_research",
            description="Collect governed evidence before answering.",
        )
    ],
)
```

The tool `name` and `description` are the model's delegation contract. Use
`agent_tool` for discretionary or repeated delegation; use a graph-control
primitive when the edge itself is part of the declared contract.

Existing URL-based sub-agent declarations remain supported for compatibility.
For new deterministic cross-app graph edges, prefer a named logical binding so
source, generated code, and topology describe the role rather than its
transport.

Identity propagation is scoped per hop: the calling user's OBO identity is for
the peer's tool and governed data access. The peer's model calls use the peer
application's service principal, not the caller's OBO token (#633). See [A2A
authentication](../multi-agent/a2a.md#app-to-app-authentication).

## Choosing a control edge

| Need | Use |
|---|---|
| Fixed order | `SequentialAgent` |
| Concurrent fan-out and gather | `ParallelAgent` |
| One semantic branch choice | `RouterAgent` |
| One lexical branch choice | `KeywordRouter` |
| Bounded local refinement | `LoopAgent` |
| Local conversational transfer | `HandoffAgent` |
| Parent-controlled discretionary delegation | `agent_tool` |
