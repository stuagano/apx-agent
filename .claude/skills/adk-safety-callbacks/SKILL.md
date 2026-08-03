---
name: adk-safety-callbacks
description: Apply Google ADK callback and guardrail patterns to apx-agent. Use when adding a guardrail, screening agent input or filtering output, redacting PII, adding an approval gate, blocking or allowlisting a tool at runtime, or rate-limiting — and when choosing which of the six lifecycle hooks fits, or whether a built-in guard supersedes a hand-written callback.
---

# ADK safety & callbacks

ADK's before/after callback model, mapped onto apx-agent's lifecycle hooks.
The hook names are ADK-compatible; both short (`before_tool`) and long
(`before_tool_callback`) forms are accepted.

## Pick the hook by intent

| Intent | Hook |
|--------|------|
| Gate/authorize/rate-limit the whole turn | `before_agent_callback` |
| Screen input (injection, PII in prompt) | `before_model` |
| Filter/block model output before the user sees it | `after_model` |
| Gate a tool call (allowlist, approval) | `before_tool` |
| Filter/redact tool output | `after_tool` |
| Audit the final response | `after_agent_callback` |

`before_model` is an input guardrail; `after_model` is an output guardrail;
`before_tool` is a tool guardrail — the same roles ADK and the OpenAI Agents
SDK use.

## Raise to abort

The pattern is identical across all six hooks: **raise an exception to abort.**
The exception message is what the LLM sees — it can recover or report it. For
`after_model`/`after_agent`, raising suppresses the response. This replaces
decorator-based guardrails; there is no `@input_guardrail`.

## Reach for built-ins first

Don't hand-write what's already declared:

- **`ToolAllowlist(["sql_query", ...])`** / **`ToolDenylist([...], message=...)`**
  as a `before_tool` hook for a fixed, well-known tool set — lighter than a
  hand-rolled callback. They raise `PermissionError`; the model adapts.
- For heavier needs (injection detection, rate limits, audit logging as
  configuration) use the built-in compliance/Watchdog guards rather than
  reimplementing them.

Hand-write a callback only for logic the built-ins don't cover.

## Async approval gates

Hooks may be async — they're awaited. Use an async `before_tool` to block
write-tools (`insert_row`, `update_record`, …) until a human approves,
raising `PermissionError` on rejection.

## Where governance supersedes a callback

Identity passthrough (OBO token) and UC grants already enforce *who can do
what* on each turn — don't reimplement authorization in a callback. Use
callbacks for content screening and policy; use governance for identity.

## See also

- Which agent/tool a callback guards: `adk-multi-agent`, `adk-tool-design`.
- Returning normal not-found messages (not raising) from a tool:
  `adk-tool-design`.

## Read next

- [Callbacks and guardrails](../../../docs/safety/callbacks.md) — all six hooks,
  signatures, wiring diagram, `ToolAllowlist`/`ToolDenylist`, common patterns.
- [Compliance](../../../docs/safety/compliance.md) — built-in guard configuration.
- [Identity passthrough](../../../docs/safety/identity-passthrough.md) —
  per-turn identity-based authorization.
