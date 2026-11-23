# Design: `user:` / `app:` keyed-state scopes (G3 phase 3)

**Status:** decided 2026-11-23 — `user:` is managed memory; `app:` is deploy-time config ([#1233](https://github.com/stuagano/apx-agent/issues/1233)). Do not add a store. ·
**Date:** 2026-06-25 (decisions 2026-10-21 and 2026-11-23) · **Source:** ADK gap audit (G3, #232) ·
**Builds on:** [keyed-shared-state.md](keyed-shared-state.md) (phases 1–3 plan),
[keyed-state-tool-access.md](keyed-state-tool-access.md) (tool read/write),
[session-state-persistence.md](session-state-persistence.md) (phase 2 seam) ·
**Parent:** [#862](https://github.com/stuagano/apx-agent/issues/862) ·
**Closes:** [#947](https://github.com/stuagano/apx-agent/issues/947)

Phase 1 shipped the in-graph `state` channel (`output_key`, `{key}` templating,
`Dependencies.State`). Phase 2 designed session-scoped persistence. This note
records where the two remaining ADK prefixes land. They do not get a key-value
store.

---

## Decision — 2026-11-23 (#947, #1233)

The 2026-10-21 check still holds: managed session and memory stores are not an
exact key-value bucket, and `ConversationStore` stays unextended. No Lakebase
table.

### `user:` is preference memory

`user:` (this principal, across sessions) is the Agent Bricks memory pipeline
plus the memory tools. It is soft, distilled memory, not `state["user:key"]`.

- Declare `memory={"type": "managed"}` and `session={"type": "managed"}`.
- The pipeline reads the managed session store and writes facts and preferences
  under each session's actor. `--trigger scheduled` runs about every 24 hours.
  The default `manual` runs only on `agentbricks memory pipeline run`.
  `--instructions` says what to keep.
- A preference the agent must keep on this turn goes through the `remember`
  tool. Later turns read it with `recall` or search. Auto-recall can place the
  hits on the system prompt. There is no exact key get.

Trade-offs:

- A scheduled pipeline is eventual. The next session can run before the next
  distillation. A manual run is the way to distill now.
- `remember` is immediate, and it is still text. A later `recall` searches;
  it does not return the value stored under a fixed key. Wording and ranking
  can miss.
- A graph write of `state["user:…"]` does not enter memory. Copying ADK state
  into the memory store automatically would pollute recall, so that path stays
  unwired. The pipeline and the memory tools are the writes.
- A checkpointer on one thread can still hold the key for that session. The
  next session is a new thread and does not have it. That is session state,
  not `user:` scope.

Author steps live in
[sessions-and-memory.md](../running/sessions-and-memory.md#cross-session-per-user-preferences).
Pipeline behaviour is
[Memory pipeline](../running/sessions-and-memory.md#memory-pipeline) and
[managed-stores.md](../running/managed-stores.md#memory-pipeline).

### `app:` is deploy-time config

`app:` (one value for every user of this agent) is agent config set at deploy
time, for example in `agent.toml` or `[tool.apx]`. Agents do not write it at
runtime. There is no shared runtime bucket, and this doc does not add one.
[#1233](https://github.com/stuagano/apx-agent/issues/1233) tracks that decision.

`{app:…}` templating is not seeded from that config today. `_render_template`
in `_compile.py` substitutes `{app:key}` only when `state["app:key"]` is
already in the in-graph dict. A missing key stays literal. The dict is filled
by a tool write, `output_key`, domain input on the turn, or the same thread's
checkpoint. `Agent`, `AgentConfig`, and `agent.toml` have no app-constant
field. `agent.toml` is the Agent Bricks manifest (framework, server, stores,
auth, tools). `[tool.apx]` is a fixed list of CLI keys. Seeding a new config
table into every invoke path is a separate change, so this PR does not wire
it and #1233 stays open.

### Host, and what a write does today

Phase 3's host is the durable session store (the managed-session checkpointer,
or `session_keys` on a composite). It is not `ApxChatAgent`. The October draft's
`_chat_agent.py` line numbers are removed; do not wire scopes through that
adapter.

`session_keys` are unprefixed keys for one session. They are not `user:` or
`app:`. `SequentialAgent` rejects a key that contains `:`.

When a graph does put `user:x` or `app:x` in `state`:

- In-graph, the prefix is only a name. `{user:…}` / `{app:…}` / `{temp:…}`
  render from the `state` dict when the key is present (`_TEMPLATE_KEY`).
- `partition_state` / `persistable_state` leave both prefixes out of
  `ConversationStore.set_session_state`. `persist_session_state` logs a
  warning when it drops them. The served adapters do not call that helper.
- A durable composite saves only its `session_keys`. A `user:` or `app:` key
  in the turn state is omitted from the snapshot, and `save_session_state`
  logs the same warning. The composite path does not use a graph checkpointer
  for those keys.
- A tool write through `StateProxy`, or an `output_key` that itself starts
  with `user:` or `app:`, logs the warning once per key per process. The
  value is not copied into memory.
- A LangGraph checkpointer keeps the whole `state` channel for that thread,
  including prefixed keys, and does not strip `temp:`. The next thread does
  not see them.

---

## Decision — 2026-10-21 (#947)

#947 asked whether `user:` / `app:` should land on managed session/memory stores
instead of new `ConversationStore.get_scoped_state` / `set_scoped_state` methods.

**Managed stores do not cover this KV.** The 2026-11-23 decision answers the
gap with memory and deploy config, not with a new table.

| Store | What it actually is | Why it is not `user:` / `app:` |
|---|---|---|
| Managed Session Store (`DatabricksSessionStoreSaver`) | LangGraph checkpointer keyed by `thread_id` + `actor_id` | One conversation (or invocation thread). Not "this principal across sessions" and not "every user of this agent". |
| Managed Memory Store | Semantic recall (`add` / `search` / `list`) partitioned by `actor_id` | Not a flat keyed dict. Putting ADK state at a memory `path` would pollute `recall()` unless every recall filtered it out. |
| ConversationStore `session_state` | JSON dict on one conversation | Session-scoped only. Storing `user:` there would pin the value to that conversation. |

**Do not implement `get_scoped_state` / `set_scoped_state`.** That would add
another Lakebase table on the path #862 is retiring, and it would not run on
the durable host.

**Shipped, in-graph only:**

- `{user:…}` / `{app:…}` / `{temp:…}` instruction templating (`_TEMPLATE_KEY`)
- `partition_state` / `persistable_state` drop `temp:` and `user:` / `app:`
  from ConversationStore `set_session_state`
- A warning when a `user:` or `app:` key is written or dropped at that boundary

The October draft's open questions (app-write opt-in, `user_id` vs email,
scoped delete) close with the 2026-11-23 decision: agents do not write `app:`
at runtime, `user:` has no KV owner key, and there is no scoped delete.

---

## What is wired

| Piece | State |
|---|---|
| `ApxState` channel, `_merge_state`, `output_key`, `{key}` templating including a `user:` / `app:` / `temp:` prefix | Live, in `_compile.py` |
| `Dependencies.State` (`StateProxy`) | Live. A `user:` or `app:` write logs the warning |
| `ConversationStore.set_session_state` | Live for one conversation |
| `persist_session_state` | Helper only. Served adapters do not call it. When called, it writes unprefixed keys and warns on `user:` / `app:` |
| Durable checkpointer | One thread. Keeps the `state` channel, prefixes included |
| `session_keys` on a durable composite | One session, unprefixed keys, managed session store. `user:` / `app:` rejected at construction and omitted if written into state |

---

## Rejected proposal (October draft)

The draft treated the prefix as a persistence route: seed `user:` and `app:`
at turn start, and merge them into a scoped table on the way out. It named
`ApxChatAgent` as the seed host and cited `_chat_agent.py` line numbers that
have since moved. Those citations are removed. Do not implement that proposal.

- ChatAgent is the compatibility host #864 is keeping. Phase 3 does not extend it.
- `session_keys` are not a stand-in for `user:` / `app:`.
- A full-dict overwrite of a cross-session owner loses concurrent writes. That
  correctness problem does not arise, because there is no cross-session KV.
- Reusing `MemoryStore` as the value path was rejected in the draft for the
  same reason the 2026-11-23 decision uses it only as recall: memory is search,
  not a flat dict. The pipeline writes prose the agent recalls. It does not
  round-trip `state["user:pref"]`.

## Out of scope

- A Lakebase table, `ConversationStore` scoped methods, or an automatic copy
  of ADK state into memory.
- Seeding `{app:…}` from `agent.toml` / `[tool.apx]` (left on #1233).
- TTLs, size caps, and scoped-key deletion.
- Typed `output_key` values ([typed output contracts](../agents/llm-agent.md#typed-output),
  #835) do not extend these scopes.
