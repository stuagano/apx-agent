# Dev UI Roadmap (`/_apx/*`)

The in-process developer environment that ships with every apx-agent: chat, trace,
tools, eval, edit, setup. Goal: keep iteration tight — write a tool, send a message,
see exactly what happened, verify against a test case, all without leaving the page.

## Shipped

- **Trace ring buffer + browser** — `/_apx/traces`, `/_apx/traces/{id}`. Python parity
  with the TS implementation. ([#16])
- **Live trace tab** — split-pane chat with span events streamed via SSE; spans render
  in real time during LLM waits, not just between text chunks. ([#17])
- **Persistent eval cases** — `evals.json` colocated with `agent_router.py`, per-case
  run/delete/edit, streaming runs that capture `trace_id` and surface a "→ trace" link.
  ([#18])

## Next up

1. **Probe page (`/_apx/probe`)** — currently redirects to `/_apx/setup`. Wire real
   checks: model auth + a one-shot LLM call, sub-agent reachability, vector search
   endpoints, required env vars. Single page, traffic-light output.
2. **Span replay** — click any tool or LLM span in `/_apx/traces/{id}` → modify
   inputs in-place → re-run that step against the same agent. Faster than restarting
   a conversation to test a tool tweak.
3. **Eval scoring beyond keyword match** — keep keyword as the default, add an
   optional LLM-judge column per case (`expected_judge: "answer is correct"`).

## Backlog

- Sub-agent topology graph in the Trace tab (when the agent calls sub-agents)
- Token / cost summary per trace
- Search + filter on `/_apx/traces` (by agent, status, span type, time)
- Hot-reload status indicator (visible flash when tools reload)
- CSV import/export for eval cases
- Run history per eval case (last N runs with diffs)
- Tool diff view in `/_apx/edit` before save
- Multi-conversation history persistence in `/_apx/agent`

## Non-goals

- Replacing MLflow / production eval frameworks. The Eval tab is for fast inner-loop
  iteration; promote to MLflow for offline/regression runs.
- Persisting traces across restarts. Ring buffer by design — keeps the dev UI
  zero-config. Long-term storage belongs elsewhere.
- Auth / multi-tenancy. The dev UI is dev-only; deployments turn it off or sit
  behind workspace SSO.

[#16]: https://github.com/stuagano/apx-agent/pull/16
[#17]: https://github.com/stuagano/apx-agent/pull/17
[#18]: https://github.com/stuagano/apx-agent/pull/18
