# Design: Declarative role pipelines are the one APX execution model

**Status:** proposed for review
**Date:** 2026-09-10
**Decision:** an APX application declares one logical route-and-pipeline graph. Every
supported conversation ingress compiles that graph. Deployment transport—including
`RemoteDatabricksAgent`, App URLs, protocol framing, and OBO headers—is internal
binding information, never a customer-facing agent or pipeline step.

## The product rule

An application has one answer to: *how does this request run?*

```text
Customer request
      |
      v
Logical router
  | direct data answer
  | pricing review: Data -> Pricing -> Opportunity -> human approval
  | document review: Evidence -> Pricing -> Opportunity -> human approval
      |
      v
Final customer-safe result
```

The router always owns the request, its caller scope, policy, trace, and final
answer. A factual answer is a terminal route through that same router; it is not a
second endpoint or a bypass around the workflow.

The customer-visible path contains only business roles, for example:

```text
Decision Router -> Data -> Pricing -> Recommendation -> Human review
```

It must never expose `RemoteDatabricksAgent`, an Apps URL, HTTP/A2A wire details,
or credential/header information. Those are deployment internals in the same sense
as a model-serving endpoint or a Unity Catalog connection.

## Why this change is necessary

APX already has the right local composition primitives:

- `RouterAgent` makes one constrained semantic routing decision.
- `SequentialAgent` represents a fixed ordered pipeline.
- `create_app()`, `compile_to_chat_agent()`, and `compile_to_responses_agent()`
  all accept one root agent and compile it through the shared graph compiler.

There are two gaps:

1. `AgentConfig.workflows` / `ExampleWorkflow` are display metadata. They render
   a topology but do not influence the executed graph. A declaration shown to a
   customer must not be a diagram beside a different execution path.
2. `RemoteDatabricksAgent` is a `BaseAgent`, but the standard compiler does not
   compile it as a deterministic `SequentialAgent` step. The existing
   `sub_agents=[url]` option instead turns a remote into an LLM-selected tool,
   which is correct for discretionary delegation but wrong for a required,
   declared workflow edge.

The result is a confusing split: an app can visually claim a workflow while its
standard chat/Responses path runs another router, and a remote can appear in an
author's source even though it is only transport.

## APX-native declaration

The application author uses a compact APX composition language, not just four
classes. The existing building blocks have distinct jobs:

| Layer | Existing APX building blocks | Declarative job |
| --- | --- | --- |
| Capability leaves | `Agent` / `LlmAgent`, `DataAgent`, `CoworkerAgent` | A named business capability that can use governed tools and return an output. `CoworkerAgent` is a specialized `DataAgent`, not a separate orchestration model. |
| Graph control | `RouterAgent`, `KeywordRouter`, `SequentialAgent`, `ParallelAgent`, `LoopAgent`, `HandoffAgent` | Choose a bounded semantic or rule-based route, run ordered steps, fan out independent work, repeat a bounded task, or make an explicit conversational handoff. |
| Discretionary delegation | `agent_tool(...)` | Let an enclosing agent decide whether to invoke a capability. This is a tool decision, not a required workflow edge. |
| Attached declarations | `@tool` and platform tool factories; guardrails, service policies, resources, memory/session configuration, callbacks, and templates | Define what a node may do, what state it keeps, and what policy applies. They attach to graph nodes or edges; they are not hidden routing systems. |

`KeywordRouter` remains a valid deterministic-rule primitive when a route is
genuinely determined by stable rules. It must not become a second semantic
coordinator beside `RouterAgent`. `RemoteDatabricksAgent` remains a compatible
low-level transport escape hatch during migration, but is not a normal product
declaration or a customer-visible node.

The common product shape below uses `RouterAgent`, `SequentialAgent`, `DataAgent`,
and ordinary `Agent` leaves because it is the smallest useful example—not because
those are the only authoring primitives. A named leaf is the stable identity of a
business capability; it is not a hostname or a remote-client class.

Illustrative shape:

```python
from apx_agent import Agent, DataAgent, RouterAgent, SequentialAgent

data = DataAgent(
    name="data",
    description="Answers governed factual market questions.",
    # ordinary DataAgent configuration
)

pricing = Agent(
    name="pricing",
    description="Turns approved data evidence into a pricing decision packet.",
)

opportunity = Agent(
    name="opportunity",
    description="Turns an approved pricing packet into an approval-required opportunity.",
)

agent = RouterAgent(
    agents=[
        ("answer", "Answer a factual question without remote delegation.", data),
        (
            "pricing_review",
            "Prepare a pricing recommendation when the caller asks for a decision or action.",
            SequentialAgent([data, pricing, opportunity], name="pricing_review"),
        ),
    ]
)
```

The direct route ends after `data`. The pricing route has exactly the declared
steps. A router may choose a route, but it may not invent an agent or jump to an
undeclared step.

There is no new public `AgentRole`, `Pipeline`, or coordinator type. The work
standardizes one executable graph made from the existing APX building blocks:
`RouterAgent` is the controlled route choice, `SequentialAgent` is an ordered
pipeline, `ParallelAgent` is a fan-out, `LoopAgent` is a bounded repeat, and
`HandoffAgent` is an explicit conversational transfer. `KeywordRouter` executes
stable declared rules, while `agent_tool(...)` is discretionary model-directed
delegation rather than a required edge. `DataAgent`, `CoworkerAgent`, and `Agent`
are capability leaves. Contract and approval metadata attach to the declared agent
or edge, never to remote transport.

The invariant is one root graph and one vocabulary across every ingress—not one
mandatory topology shape. A fixed review flow uses `SequentialAgent`; independent
evidence collection may use `ParallelAgent`; a true conversation transfer may use
`HandoffAgent`. Every selected path is still declared, governed, traceable, and
customer-safe.

## Deployment binding is internal

At setup/compile time, each non-local named agent resolves to a deployment binding.
The source declaration remains APX-native:

```toml
[tool.apx.agent.bindings]
pricing = "$PRICING_APP_URL"
opportunity = "$OPPORTUNITY_APP_URL"
```

The resolver materializes the correct local implementation or remote transport.
For a remote binding, it may internally use `RemoteDatabricksAgent`, discover an
Agent Card, forward OBO identity, and inject trace context. None of those values
become a public agent name, route label, topology node, trace span name, or
customer response field.

An unresolved named agent that is required by a declared route fails closed during
setup with a clear operator-facing error. It must not silently use a different
route, fall back to an arbitrary tool, or expose the binding failure to a customer.

The existing hand-authored `RemoteDatabricksAgent` and `sub_agents` APIs remain
compatible lower-level escape hatches during migration. They are not the normal
authoring model for product workflows and are not used by the generated/published
customer topology.

## One compiler path for every ingress

All ingress adapters receive the same resolved root declaration:

```text
root chat UI         ┐
/invocations         ├─ request/response adapters only ─> compile declared root
/responses           ┤                                      |
A2A message/send     ┘                                      v
                                                  logical route + named agents
```

No endpoint gets its own `KeywordRouter`, custom coordinator, or manually
reconstructed workflow. A custom product HTTP endpoint may format a richer
business response, but it calls the same root graph and does not make an
independent routing or handoff decision.

This is the standard for every product deployment: the visible decision router is
the root declaration. Its direct Data route and multi-step pricing/document routes
are branches of that declaration, regardless of whether the caller arrives through
the browser, the standard Responses protocol, `/invocations`, or A2A.

## Contracts, governance, and approval

The declaration is authoritative only if its policy is executable:

- A step may declare an input and output contract. A bound remote capability must
  prove compatibility from its published schema/card before the route becomes
  available.
- A declared sequence may only move to its next declared role. It may not fan out
  or call an undeclared remote based on text returned by a model.
- Caller scope, read-only policy, deadlines, and hop limits travel with the root
  context and apply to both local and remote role execution.
- A role marked approval-required can prepare a packet but cannot perform the
  external write. The final status remains approval-required until a separate
  authorized action is taken.

The first implementation must not migrate a typed task/artifact workflow to
untyped conversational text. If the generic layer cannot validate a declared
`Evidence.v1 -> Recommendation.v1 -> ApprovalPacket.v1` chain, that validation is
a prerequisite of migration—not a detail to defer.

`ExampleWorkflow` remains example/display data. It must not be repurposed as the
runtime source of truth; its question and UI-copy fields are not execution policy.

## Observability and customer experience

Every request gets one logical root span and a route made of declared agent IDs:

```text
decision-router
  └─ pricing_review
       ├─ data
       ├─ pricing
       └─ opportunity
```

For a remotely bound agent, APX continues the active MLflow trace across the wire
and names the child span after the declared agent. The internal binding can add safe
diagnostic attributes for operators, but never substitutes tag-based trace
reassembly for parent/child trace context.

The normal customer experience shows bounded, useful progress:

- selected route;
- named agents that ran;
- status, evidence/provenance, and approval state;
- a safe failure or next action when a route cannot complete.

The customer experience does not show private model reasoning, raw tool arguments,
remote URLs, access tokens, or remote-client implementation names. A technical
operator drawer may reveal safe deployment diagnostics under appropriate access
control, separately from the customer-facing route.

## Public-example hygiene

APX public documentation, examples, templates, generated source, and user-visible
CLI copy must use generic platform examples. They must not include a customer name,
workspace URL, account identifier, customer-specific environment-variable prefix,
or customer-specific contract label.

This is enforced as a small repository check over the public artifact surface:

- `README.md`, `docs/`, and `python/examples/`;
- source templates and CLI strings that APX generates or presents to users; and
- this design document itself.

The first deny-listed private term is held in the test-only guard, not in public
example material. The list stays deliberately narrow and is extended only when a
real leakage is found. The check must fail a pull request before an internal
customer example reaches generated code, documentation, or a customer-facing UI.

This guard is a backstop, not permission to use real customer context during
authoring. Reviewers still remove customer-specific language at the source.

## Non-goals

- No free-form peer-to-peer agent mesh.
- No second coordinator framework or endpoint-specific workflow code.
- No replacement of `RouterAgent` / `SequentialAgent` with a parallel pipeline
  language.
- No automatic conversion of every existing `sub_agents` tool delegation into a
  deterministic pipeline step.
- No hiding of policy failures behind a synthetic success response.
- No breaking removal of the low-level remote API in the first migration.

## Acceptance criteria

1. **One declaration, all ingress.** The same declared root handles root chat,
   `/invocations`, `/responses`, and A2A `message/send`; normalized logical route
   and final business result agree.
2. **Direct is coordinated.** A factual request produces `router -> data`, makes
   zero remote calls, and returns a terminal result through all ingress paths.
3. **Declared workflow is exact.** A decision request produces exactly
   `router -> data -> pricing -> opportunity`; every remote invocation corresponds
   to a declared agent and edge.
4. **No transport leaks.** Public topology, progress events, trace labels, and
   final result contain declared agent names only—not `RemoteDatabricksAgent`, raw URLs,
   protocol names, OBO headers, or credentials.
5. **Contracts remain real.** A remotely bound agent whose advertised input/output schema
   cannot satisfy its declared contract is rejected before execution; an
   undeclared transition is rejected during execution.
6. **Governance remains real.** Scope, read-only policy, deadline, hop limit, and
   approval-required status survive a remotely bound agent boundary.
7. **One trace tree.** An end-to-end remote workflow has one MLflow trace ID with
   logical parent/child spans, verified from parentage rather than shared tags.
8. **Compatibility is intentional.** Existing direct use of `RemoteDatabricksAgent`
   and `sub_agents` continues to work while documented product workflows migrate
   to the APX-native graph.

## Migration boundary

The first code slice is deliberately narrow:

1. Resolve named agents at the existing setup/compiler seam.
2. Teach the shared compiler to execute a bound remote agent as a deterministic
   declared step.
3. Make topology and tracing report the declared agent, not the transport.
4. Add one black-box proof that exercises the same declaration through standard
   ingress and an actual remotely bound agent.

Only after that proof should a product delete its parallel keyword-router/endpoint
behavior and move its declared typed workflows onto the APX graph. This keeps the
framework honest: a declared pipeline is executable before a customer product
depends on it.
