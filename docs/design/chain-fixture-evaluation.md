# Recorded-tool chain evaluation (#840)

Approved mode: run the real model against recorded tool results. Keep the
existing trace-coverage mode of `evaluate_chain` unchanged. Add an explicit
`fixtures=` mode returning the same report with per-step scores and outcome counts.

Each JSON case has `id`, `request`, ordered `steps`, `expected_outcome`
(`correct` or `escalated_with_evidence`), and optional `expected_failure`
(`step` path and `reason`). Each step has `path`, `tools` (name, args, output),
and `expected_output` for successful steps. Cases target named sequential leaves
and nested sequences. Every successful leaf must declare `output_schema`.
An expected failure includes its step but no expected output; later steps are
excluded. Correct cases cover all leaves in execution order. Escalation cases
cover the exact prefix ending at the failed leaf.

All fixtures and paths are validated before compiling or calling a model.
Reject unknown/duplicate paths, ambiguous unnamed/repeated sibling names,
unsupported graph types, remote bindings, deferred tools, non-JSON data,
and conflicting expectations. Expected successful outputs must satisfy the
actual leaf schema. No Python classes or code are loaded from fixture files.

Use the existing compiler. Replay registers the original tool input schemas
without resolving live dependencies, then supplies recorded results through
tool middleware. Match exact JSON tool name/arguments within each step;
repeated identical calls consume successive recorded results. All records must
be consumed. Unexpected calls, wrong args, missing calls, and exhausted records
fail the case with safe diagnostics. Never fall through to a real tool.
Guardrails, validation, and escalation run normally; model calls and author
callbacks remain real. This simulates tool results, not authorization or tool
state mutations. It is not a sandbox for arbitrary author code.

Record immutable validated step outputs through an invocation-local compiler
observer. Do not mutate the author's declarations or failure policy. Do not
use global monkeypatching or correlate historical traces. Isolate each case,
including after an exception or timeout, and freeze observations at completion.

`correct` requires every expected step and tool call to pass, with no failure.
`escalated_with_evidence` requires the expected terminal failure path/reason,
strictly valid escalation metadata, at least one completed validated evidence
entry, evidence equal to observed correct prior outputs, and no downstream
execution. Any mismatch yields `wrong`, including unexpected escalation or a
plausible final answer after a bad intermediate result. An escalation at the
first step has no completed evidence and cannot earn this outcome.

Return per-case ID, response, observed/expected step data, step diagnostics,
outcome and duration; aggregate outcome counts. Fixtures do not silently use
MLflow judges, tracking, user tokens, or live evalsets. No new dependencies,
UI, CLI, retries, trace recorder, model-output replay, or live deployment.
Documentation and a labeled synthetic fixture example ship with the code.
