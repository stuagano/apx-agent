# Design: session state for durable composites (`session_keys`, `halt_statuses`)

**Status:** implemented · **Date:** 2026-10-27 · **Builds on:**
[session-state-persistence.md](session-state-persistence.md),
[sequential-escalation.md](sequential-escalation.md),
[managed-state-contract-map.md](managed-state-contract-map.md)

## Intent

A typed `durable_agent_server` pipeline should accept a person typing at it. In the
ADK pattern, an untyped intake step turns the conversation into the typed request,
asks a follow-up when slots are missing, and remembers slots across turns. The
downstream typed steps run only once the request is complete.

apx can't do that today:

- a durable composite (Sequential, Router and the rest) runs **stateless per
  request**: `_compile.py` refuses a checkpointer for any non-`LlmAgent` root, and
  `inspect_target` drops the auto-derived managed store;
- a sequence can't stop early on a domain status: only failure escalation stops it.

Success means:

- an intake step's slots survive across turns of one session (same `session_id`,
  same user), stored server-side in the managed Session Store;
- nothing else in the turn's state carries over: earlier requests, market payloads
  and other `output_key` values never leak into a later turn;
- a sequence stops after a step whose declared status says it needs input, and
  that step's message is the turn's answer;
- typed callers that send `apx_data_parts` see no change.

## API

```python
SequentialAgent(
    [intake, lookup, score, report],
    session_keys=("intake",),        # state keys persisted across turns of a session
    halt_statuses=("needs_input",),  # stop after a step whose status is one of these
)
```

- `session_keys`: unprefixed state keys (the same scope rule as `persistable_state`;
  `user:` and `app:` are rejected). Allowed on a durable composite root. On a root
  `LlmAgent` it's an error, because that agent already has the full checkpointer.
- `halt_statuses`: after each step, if the step declares `output_status_path` and
  the value there is one of these, the sequence ends. The step's assistant message
  is the final answer, and no later step runs. A halt is a normal completed turn,
  not an escalation. If the step also declares `output_message_path` (for example
  `("question",)`), the sequence ends with that field's text as a plain assistant
  message, marked `apx_answer`. The native chat page shows only that text;
  the step's exact JSON stays in its recorded output.
  `output_replies_path` (requires `output_message_path`) names a list of 1-4 strings
  of at most 40 characters on the same output. The answer then carries them as
  `apx_replies`, and the native chat page shows each as a button that sends its text
  as the next message. Anything else in that field is ignored.

## Behaviour

**Load** (start of a durable turn, in `_durable_agent.execute`). The state thread id
is `session_thread_config(agent, session_id, user)["configurable"]["thread_id"]`
plus the suffix `"/apx-state"`. That id is already scoped by agent, user and session
(sha256), so user A can't read user B's state. apx reads the latest snapshot item
from that managed Session and seeds only the `session_keys` into the graph's `state`
channel. If there's no session yet, it starts empty.

**Save** (end of a turn whose status is `completed`, halted or not). apx appends one
snapshot item, `{k: final_state[k] for k in session_keys if k in final_state}`. Failed
or interrupted turns don't save. Keys not on the list are never written.

**Compaction.** Once a session holds more than 20 snapshot items, apx runs
`clear_items` and then appends the latest snapshot.

**Store access** uses the SDK's `SessionStoreClient` (`get_session`, `create_session`,
`append_items`, `list_items`, `clear_items`) with the App's managed store
(`AGENT_SESSION_STORE`). That's the same store the LlmAgent path uses, under a
separate thread id. Composites with `session_keys` must have that store configured;
compile fails loudly otherwise.

**Unchanged and still refused for composites:** a full graph checkpointer, durable
recovery, and approvals/interrupts.

## Risks

- **Request-user auth is required** when `session_keys` are declared. Without it, state
  would be keyed by a caller-chosen `session_id`.
- **Compaction is clear-then-append.** A crash between the two calls loses that
  session's slots, and the next turn starts empty.
- **Last-write-wins.** There's no compare-and-set. The SDK serializes invocations
  per session, which mitigates concurrent writes; a concurrent write from an older
  worker could drop a slot update. Acceptable for intake slots.
- **A new state contract.** The snapshot item shape is
  `{"type": "apx_session_state", "state": {...}}`, recorded in the managed-state
  contract map.
- **The intake step reads only its persisted slots plus the new message,** not the
  whole session history. That keeps the context small, and remote child steps never
  receive prior turns.

## Testing

- Unit tests with a fake `SessionStoreClient`:
  - slots accumulate over 3 turns;
  - a non-listed `output_key` from turn 1 is absent in turn 2;
  - different users get different state;
  - a failed turn persists nothing;
  - compaction runs after 20 items;
  - a missing store fails at compile;
  - `user:`/`app:` keys are rejected;
  - an `LlmAgent` root with `session_keys` is rejected.
- Halt: a step with status `needs_input` ends the sequence with its message, and a
  later step's tool doesn't run. A non-halting status continues. Nested sequences
  halt the outer sequence too.
- The existing composite tests still pass: no checkpointer, no recovery.
- A real-browser test: type a vague prompt → follow-up question → answer → the
  chain runs.

## Delivery

1. apx PR: `session_keys` + `halt_statuses`, docs, and the contract-map entry.
2. A consuming project adds an untyped intake step; identity and tenant fields come from the caller's authorized scope, never the model.
