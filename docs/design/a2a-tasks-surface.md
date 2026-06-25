# Design: A2A `/tasks/*` task-execution surface (MVP) (#284)

**Status:** approved (build greenlit 2026-06-24) · **Date:** 2026-06-25 ·
**Source:** #284

The agent already serves an **A2A discovery card** at `/.well-known/agent.json`
(`AgentCard`, `_models.py:390`) advertising `protocolVersion 0.3.0`,
`capabilities.streaming = true`, `capabilities.multiTurn = true`. Nothing backs
those claims over the A2A protocol — the agent only runs synchronously through
MLflow `/invocations` (`_invocations.py`). An external A2A client that trusts the
card gets a 404 on the actual protocol. This phase makes the card honest by
serving the A2A task surface.

---

## MVP scope (this phase)

A2A v0.3.0 is **JSON-RPC 2.0 over HTTP POST** to the agent's service URL. The MVP
implements the three core methods, **sync-complete** (run to completion, return a
terminal `Task` — no background workers, no async `working` state):

- **`message/send`** — accept a `Message`, run the existing agent to completion,
  return a `completed` `Task` (reply as an artifact + in history).
- **`tasks/get`** — fetch a stored `Task` by id (`-32001` TaskNotFound on a miss).
- **`tasks/cancel`** — sync-complete tasks are already terminal, so this returns
  `-32002` TaskNotCancelable for a known task (`-32001` if unknown). Honest for
  the MVP; real cancellation arrives with async working-state.

Unknown methods → JSON-RPC `-32601` (Method not found).

### Deferred (follow-ups, noted so the card's gap is explicit)

- **`message/stream`** (SSE) — `capabilities.streaming = true` still over-claims
  until this lands. Reuse the existing streaming agent path (`_stream_chunks` in
  `_invocations.py`). Next slice.
- **Async working→completed** — needs a task runner + non-terminal state; only
  then is `tasks/cancel` meaningful.
- **`tasks/pushNotificationConfig/set|get`** — webhook callbacks. Phase 2.

## Settled design choices (from the issue)

1. **Sync-complete**, not async working-state — smallest correct MVP, no workers.
2. **Task storage = in-process bounded ring** (`OrderedDict` + lock, mirroring
   `_trace_store.py`). Per-replica/ephemeral is fine for MVP; `tasks/get` is a
   best-effort recent-task lookup, same posture as the trace buffer.
3. **Backing execution = the SAME path `/invocations` runs.** Build one
   `chat_agent_for(agent, model=config.model, conversation_store=store,
   agent_id=config.name)` at mount; `message/send` calls `chat_agent.predict(...)`.
   No forked execution.
4. **Transport / endpoint = POST `/`.** The card already advertises `url == base`
   (`_wiring.py:552`, asserted in `test_wiring.py:281`); A2A clients POST JSON-RPC
   to that URL. Root `/` is **GET-only** today (the chat UI, `_ui_root_chat.py:219`),
   so a `POST /` A2A handler coexists by method with no card change.
5. **Auth = OBO threaded like `/invocations`** via `extract_obo_headers`
   (`_obo.py`), passed as `custom_inputs` into `predict`.

## Mapping A2A ↔ the existing agent

| A2A | apx |
|---|---|
| `Message.parts[TextPart].text` (role `user`) | concatenated → one `ChatAgentMessage(role="user", content=…)` |
| `Message.contextId` | bridged to `custom_inputs["session_id"]` → multi-turn via the conversation store (reuses the `/invocations` session bridge) |
| agent reply (`ChatAgentResponse.messages` last assistant text) | `Task.artifacts[0]` (TextPart) **and** appended to `Task.history` as a `role="agent"` Message |
| run-to-completion | `TaskStatus.state = "completed"`; an exception → `"failed"` with the error as the status message |

## Models (`_a2a_models.py`, A2A v0.3.0 camelCase for real-client interop)

- `TaskState` (enum): `submitted | working | input-required | completed | canceled
  | failed | rejected | unknown` (MVP emits `completed` / `failed`).
- `TextPart` `{kind:"text", text}`; `Message` `{role, parts, messageId, taskId?,
  contextId?, kind:"message"}`; `Artifact` `{artifactId, parts, name?}`;
  `TaskStatus` `{state, timestamp?, message?}`; `Task` `{id, contextId, status,
  history, artifacts, kind:"task"}`.
- JSON-RPC envelope: `JsonRpcRequest {jsonrpc, id, method, params}`,
  `JsonRpcSuccess {jsonrpc, id, result}`, `JsonRpcError {jsonrpc, id, error:{code,
  message, data?}}`.
- Param models: `MessageSendParams {message, configuration?}`,
  `TaskQueryParams {id, historyLength?}`, `TaskIdParams {id}`.

Permissive where the client owns the shape (`configuration`, part contents),
strict on the envelope (a malformed JSON-RPC request → `-32600`; bad params →
`-32602`).

## Errors (JSON-RPC + A2A codes)

| Condition | Code |
|---|---|
| Not valid JSON | `-32700` parse error |
| Missing `jsonrpc`/`method` | `-32600` invalid request |
| Unknown method | `-32601` method not found |
| Bad `params` for the method | `-32602` invalid params |
| Agent not configured (no `agent_context`) | `-32603` internal (or A2A unavailable) |
| `tasks/get` unknown id | `-32001` TaskNotFound |
| `tasks/cancel` terminal/known id | `-32002` TaskNotCancelable |

JSON-RPC transport errors are HTTP 200 with an `error` body (per spec); the HTTP
layer only 4xx/5xx on framing failures.

## Files

- `_a2a_models.py` — the Pydantic models above (sibling to `_models.py`).
- `_a2a.py` — the bounded `TaskStore`, the JSON-RPC dispatch, and
  `mount_a2a_route(app, agent, config, conversation_store)`.
- `_wiring.py` — call `mount_a2a_route(...)` right after `mount_invocations_route`
  (line 789), same `agent`/`config`/`_store` in scope. (The Databricks Apps target
  `mount_mcp_endpoints` gets A2A in the streaming follow-up.)

## Testing

- **Models unit:** round-trip a `Task`/`Message` through JSON (camelCase keys).
- **`message/send` integration (no live LLM):** a fake agent whose reply is fixed;
  assert the JSON-RPC result is a `Task` with `state="completed"`, the reply in
  `artifacts` and `history`, and that the task is then fetchable via `tasks/get`.
- **`tasks/get`:** known id → the task; unknown id → `-32001`.
- **`tasks/cancel`:** known terminal id → `-32002`; unknown → `-32001`.
- **JSON-RPC framing:** bad JSON → `-32700`; missing method → `-32600`; unknown
  method (`message/stream`) → `-32601`; bad params → `-32602`.
- **No agent context:** dispatch returns the internal error, never 500s.
- **multiTurn:** two `message/send` calls sharing a `contextId` thread through the
  conversation store (turn 2 sees turn 1) — mirrors the `/invocations` session test.
- **Reality/ctk:** assert the card's `capabilities` claims now have a live backing
  method (a `message/send` round-trip), closing the "card writes a check it can't
  cash" gap this issue names.

## Out of scope

- `message/stream`, async working-state, push notifications (deferred above).
- Multi-replica durable task storage (in-process bounded ring is the MVP, like
  `_trace_store.py`).
- Advertising the exact transport in the card via 0.3.0 `preferredTransport` /
  `additionalInterfaces` — `url == base` + `POST /` suffices for the MVP; revisit
  if a client needs explicit transport metadata.
