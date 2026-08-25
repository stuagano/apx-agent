# Example Workflows in the APX Default Pages

## Status

Approved direction: shared APX workflow contract with Curinos as the first
consumer. The contract is additive and remains domain-neutral.

## Problem

The APX default pages currently expose useful pieces of the system but do not
make the business flow legible as one experience:

- Chat can show starter prompts from `AgentConfig.examples`.
- Topology renders the declared agent/tool graph and highlights the last trace.
- Discover shows data and wiring opportunities.
- A trace can prove that a route executed, but the UI does not explain why a
  sample question followed that route or what each handoff produced.

Curinos has a second, application-owned topology that declares business
stages, contracts, questions, and approval gates. Its example questions are
currently descriptive metadata rather than executable walkthroughs. This
creates two views of the same system and forces a reviewer to infer the
relationship between a question, the route, and the resulting artifact.

## Goals

1. Make the APX default Chat and Topology pages a runnable explanation of how
   coordinated business questions are answered.
2. Let an agent declare reusable example workflows without embedding customer
   vocabulary in the APX SDK.
3. Show the declared route before execution and the observed route after the
   existing trace/highlight mechanism records a run.
4. Explain each handoff in plain language, including the input contract, the
   output artifact, and why the next stage is eligible.
5. Preserve existing `examples` starter prompts and the existing topology JSON
   contract for agents that do not declare workflows.
6. Make Curinos the first end-to-end example with pricing, document-led, and
   relationship-opportunity questions.

## Non-goals

- No new dispatcher, routing engine, or execution path. The existing Chat,
  `/responses`, coordination API, A2A handoffs, and trace store remain the
  source of truth.
- No LLM-generated explanation of a route. Route descriptions and handoff
  explanations are declared metadata; execution status comes from the trace.
- No automatic inference of business workflows from arbitrary traces.
- No customer data in the SDK or in default examples. Curinos continues to use
  its synthetic fixture boundary for the demo.
- No replacement of the Curinos account workspace. The shared APX pages become
  the canonical technical walkthrough; the customer page remains the account
  experience.

## Design

### 1. Add a generic workflow declaration

Add an optional `workflows` field to `AgentConfig`. It is loaded from the
existing `[tool.apx.agent]` configuration envelope and is available to both
the Chat landing page and the Topology endpoint.

The serialized shape is intentionally small:

```json
{
  "id": "pricing-review",
  "title": "Pricing review",
  "question": "How is the savings product priced against peers?",
  "purpose": "Move from a market signal to an approval-ready pricing decision.",
  "route": ["intelligence", "calibrate", "compound"],
  "handoffs": [
    {
      "source": "intelligence",
      "target": "calibrate",
      "input_contract": "market signal",
      "output_contract": "pricing decision",
      "explanation": "Calibrate compares the scoped product with peer pricing."
    }
  ],
  "outcome": "Pricing packet ready for human review",
  "follow_ups": ["What evidence supports the pricing recommendation?"]
}
```

Required fields are `id`, `title`, `question`, `purpose`, and `route`. The
handoff and follow-up arrays default to empty. `route` uses application-owned
logical stage IDs, not private React Flow node IDs. The application can map
those IDs to graph nodes when it owns a semantic topology; otherwise APX shows
the declared route as a route strip and uses the existing trace mapping for
observed highlights.

`AgentConfig.examples` remains supported. When workflows are present, their
questions are merged into the starter prompt list without duplicates. Existing
agents therefore get the current landing page unchanged.

### 2. Extend the topology response additively

The built-in `/_apx/topology.json` response gains an optional top-level
`workflows` array. `TopologyResponse` remains forward-compatible and existing
nodes and edges are unchanged. Each workflow includes a runtime status field
computed by the UI from the last trace:

- `declared` — configured but not executed in the current trace;
- `active` — the selected example is running;
- `completed` — the observed trace contains the declared route or its mapped
  node/edge IDs;
- `partial` — some declared stages were observed;
- `failed` — a declared stage emitted a failure event.

The server provides declarations only. The UI must not mark a route executed
because it was declared.

For application-owned semantic topologies, the existing additive annotation
helper gains an optional workflow collection so Curinos can serve the same
contract from `/api/coordination/topology`. The helper remains non-mutating and
does not require APX to understand Curinos's manifest.

### 3. Make the default pages tell one story

#### Chat (`/_apx/agent`)

Keep the current starter chips, but render workflow chips with their title and
purpose. Clicking one sends the exact declared question through the existing
Chat path. No second invocation API is introduced.

#### Topology (`/_apx/topology`)

Add an “Example workflows” strip above the graph. Each card shows:

- business question;
- expected route;
- expected outcome;
- a `Run example` action;
- a `View route` action that selects/highlights the declared path without
  claiming it executed.

The existing ChatDock remains the execution surface. When a run completes, the
last-trace highlight remains authoritative and the selected workflow card gains
the observed status.

Add a workflow detail panel beside the existing node inspector. It renders a
step-by-step explanation from the declaration and the observed tool/handoff
events:

```text
Question
  ↓
Intelligence — established the market signal
  ↓ market signal → pricing decision
Calibrate — compared the product with peer pricing
  ↓ pricing decision → opportunity packet
Compound — identified the next relationship action
  ↓
Human review — approval required
```

If a trace is unavailable, the panel explicitly says “declared route, not yet
run.” It never converts a planned route into a false execution claim.

#### Discover and other pages

No new navigation page is needed. The default nav remains stable. Discover can
link a wired agent back to its example workflows, but wiring behavior is out of
scope for the first slice.

### 4. Curinos as the reference implementation

Curinos declares three workflow examples from its existing coordination
manifest rather than duplicating them in HTML:

| Workflow | Question | Route | Outcome |
|---|---|---|---|
| Pricing review | How is the selected savings product priced against peers? | Intelligence → Calibrate → Compound | Reviewable pricing/opportunity packet |
| Document-led review | What approved source evidence should inform this pricing decision? | Evidence → Calibrate → Compound | Scoped evidence and pricing packet |
| Relationship opportunity | Which relationships should we review next? | Intelligence → Compound | Prioritized opportunity packet for human review |

The Curinos custom topology continues to expose its logical dispatcher stages,
typed contracts, and account scope. Its response uses the shared workflow shape
so the custom account page and APX default Topology can describe the same
question consistently.

Questions sent from the Curinos account page continue to include the selected
`institution_id`. The APX default pages do not invent or accept customer scope;
they use the agent's existing authenticated context and the declared synthetic
fixture behavior where configured.

## Execution and state

The existing trace polling remains the only execution-status mechanism. The
workflow selection is client state and may be stored in the current page only;
it does not become durable agent memory. On refresh, the topology reloads
declarations and the last trace, then derives status again.

The existing `/responses` stream continues to provide answer text and tool-call
events. The workflow panel may display those bounded event names, but it must
not expose tokens, raw authorization context, or unbounded tool arguments.

Errors remain local to the selected workflow run. A failed invocation shows the
error state and trace correlation link without changing the declared route or
other workflow cards.

## Compatibility and safety

- `workflows` is optional and additive.
- `examples` keeps its current type and behavior.
- `TopologyResponse` accepts agents with no workflow field.
- Workflow metadata is static application configuration; it is not a data
  result and must not be treated as authorization.
- Execution evidence comes only from the existing trace/response path.
- Curinos synthetic account scope remains fail-closed outside declared fixture
  accounts; production UC/OBO scope remains runtime-derived.
- All user-visible text must be escaped by the existing UI rendering helpers.

## Testing strategy

### APX SDK

- Validate workflow config parsing, required fields, defaults, and duplicate
  starter-prompt merging.
- Verify `/_apx/topology.json` includes workflows without changing the base
  nodes/edges for agents that omit them.
- Verify workflow status is `declared` before a run and becomes `completed`,
  `partial`, or `failed` only from trace evidence.
- Verify Chat landing renders workflow titles/questions and Topology renders the
  route strip and plain-English handoff details.
- Run the existing topology, chat, dev-UI route, and packaging tests.

### Curinos

- Verify the three manifest workflows serialize into the shared shape.
- Verify each workflow question reaches the expected coordination route.
- Verify account scope is preserved for the relationship example.
- Verify the custom account page and `/api/coordination/topology` remain
  backward-compatible.
- Run the existing Curinos coordination, ask, overview, and compile checks.

## Rollout

1. Merge the APX additive contract and default-page UI.
2. Pin the resulting APX revision in Curinos and add the three workflow
   declarations.
3. Deploy Curinos with the existing synthetic configuration.
4. Run read-after-write checks for Chat, Topology, the three examples, and the
   account-scoped relationship question.

The feature is safe to roll back by removing the optional workflow declaration
or reverting the APX pin; agents without the field retain the current behavior.

## Decision

Use the shared declarative workflow contract. Do not build a Curinos-only
walkthrough or infer business flows from traces.
