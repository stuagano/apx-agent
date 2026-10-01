# Opt-in sequential escalation (#841)

Approved API: `Agent(timeout_s=30)` and `SequentialAgent([...], on_failure="escalate")`.
The default sequence policy is `"raise"`; an omitted timeout is unbounded.
Timeout values must be positive finite numbers, excluding booleans.

The compiled sequence owns failure routing. Opted-in direct `run` and `stream`
use that same runtime. A child deadline includes its model/tool loop, output
validation, and success hooks. Deadline expiry, an explicit top-level
`availability="unavailable"` tool return, or a schema validation failure stops
the sequence. Governance rejections, approvals, external cancellation, and
unexpected exceptions keep their existing behavior. No automatic retry occurs.

The final escalation is JSON text plus an object-valued A2A DataPart. Its
fields are `status="escalated"`, `availability="unavailable"`, `capability`,
`error`, `reason` (`timeout`, `unavailable`, `schema_miss`), `failed_step` (a
path of sequence/step names), and `evidence` (completed validated outputs).
Errors use safe diagnostics rather than copying raw tool exceptions or rejected
model output. The DataPart schema hint is `urn:apx:sequential-escalation:v1`;
receivers validate the fixed local schema, never load schemas from the hint.

Each invocation resets internal escalation/evidence channels. Only completed
typed outputs from that invocation become evidence. Failed-step keyed state
and output are discarded. Nested sequence escalation stops outer downstream
steps too. Remote escalation preserves its packet and bypasses success-schema
publication, after output guardrails have inspected the packet.

Tool unavailability is intercepted before another model turn. No prose search
or arbitrary nested-dictionary search is used. Local tool results and explicit
remote DataParts are the supported structured signals. A text-only remote peer
must adopt this contract to propagate typed failure.

Synchronous tools reuse the existing cancellable worker implementation with
the remaining step budget. Cancellation requests stop waiting and discard late
results; arbitrary synchronous code and remote side effects may continue.
There is no rollback or hard-kill guarantee. Inputs to protected steps are
isolated so late mutable-state writes cannot alter accepted chain evidence.

Parallel/loop/handoff compositions containing step policies are rejected in
this increment rather than silently dropping failure routing. Routing to a
sequence remains supported. Existing declarations without step policies are
unchanged. No new dependencies, retry policy, or escalation agent is added.

Verification covers sync/async execution, direct run/stream, typed unavailability
before a second model turn, nested propagation, late worker state isolation,
governance and unexpected-error preservation, remote DataParts, and served
Responses/ChatAgent output. Public guides and this design ship with the code.
