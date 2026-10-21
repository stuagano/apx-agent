# Managed state contract map (#854)

**Status:** evidence record, not an implementation change · **Date:** 2026-10-13 ·
**Issue:** [#854](https://github.com/stuagano/apx-agent/issues/854) (managed
state 1/6) · **Parent:** [#862](https://github.com/stuagano/apx-agent/issues/862)

This page is the planning prerequisite for the managed-state series
[#855](https://github.com/stuagano/apx-agent/issues/855)–[#859](https://github.com/stuagano/apx-agent/issues/859).
It records the existing APX persistence contracts, the AgentKit / Mason surface
this checkout actually depends on, and the gaps that later issues must close.
It does not provision stores, migrate data, or change runtime behavior.

Evidence is this checkout, the locked
`databricks-agentbricks==0.4.0` / `databricks-sdk==0.146.0` packages, the
closed [#331](https://github.com/stuagano/apx-agent/issues/331) transcript
decision, a **read-only** probe of the `fevm` profile on 2026-10-13, and a
disposable write/provision probe on profile `DEFAULT`
(`fevm-serverless-stable-7def3z`) on 2026-10-21 (#855/#856). Retention and
measured cost remain unknown. Existing `van-bsmmyz-*` stores were not
modified.

## What this issue is for

Issue 854 asked for three things before any further managed-state work:

1. Record a released SDK that contains the required methods, and verify
   availability / prerequisites / permissions / limits / retention / cost /
   release status on an explicitly selected profile.
2. Map every ConversationStore, MemoryStore, session-state, and checkpoint
   operation onto that service, including Python versus TypeScript.
3. Keep transcript ownership consistent with #331, and treat actor IDs as
   partition keys rather than authorization grants.

The first item is partly closed. The installed SDK methods are recorded
below, and a read-only `fevm` probe confirmed the APIs are enabled there.
Writes, cross-actor content, retention, and measured cost are still open.

## What is already shipped (do not re-implement)

These statements supersede the original 854 planning text, which still
describes a UC REST memory client that #866 replaced.

| Claim | Evidence |
|---|---|
| Managed memory is AgentKit, not `/api/2.1/unity-catalog/memory-stores` | [`_memory_managed.py`](../../python/src/apx_agent/_memory_managed.py) constructs `AgentKitClient(workspace_client=ws)` and uses `client.memory_stores.get / create` plus entry `add / get / list / search / update / delete`. |
| Native durable agents bind a managed Session Store as a LangGraph checkpointer | [`_durable_agent.py`](../../python/src/apx_agent/_durable_agent.py) `compile_to_durable_agent_server` constructs `databricks_agentkit.langgraph.session_store.DatabricksSessionStoreSaver`. |
| Doctor distinguishes local SDK, denied, unavailable, and unverified | [`_doctor.py` `check_agent_prerequisites`](../../python/src/apx_agent/_doctor.py) — quoted in [Doctor states](#doctor-states). |
| Legacy UC three-part memory names are rejected, not remapped | `validate_memory_store_name` in `_memory_managed.py`. |
| Request-user recovery is rejected | [`_durable_agent.py`](../../python/src/apx_agent/_durable_agent.py) and [`docs/running/runtime-targets.md`](../running/runtime-targets.md). |

No deployment, deletion, or data migration is authorized by this document.

## SDK and release contract

Recorded from this checkout on 2026-10-13. `uv run --frozen` in `python/`
resolved:

| Package | Declared | Locked / installed | Role |
|---|---|---|---|
| `databricks-agentbricks` | extra `agentbricks`: `>=0.4.0,<0.5` in `python/pyproject.toml` | **0.4.0** (wheel uploaded 2026-10-02) | Ships the `databricks_agentkit` Python package used at runtime. |
| `databricks-sdk` | `>=0.74.0` | **0.146.0** | `WorkspaceClient`; includes `WorkspaceClient.mason`. |
| `databricks-ai-bridge` | transitive via agentbricks `[memory]` | **0.22.0** | Not imported by APX memory code. |
| `databricks-agents` | dev extra `>=1.12.0` | **1.12.0** | Evaluation / serving helpers; not the managed-state client. |
| `databricks-agentkit` as its own PyPI name | not declared | not installed | Import path is `databricks_agentkit` **inside** `databricks-agentbricks`. |

`AgentKitClient` documents its own stopgap:

> `WorkspaceClient.mason` will replace the private transport once the
> generated SDK is released; the resource-oriented public surface remains
> unchanged.

APX currently talks to AgentKit (`AgentKitClient` +
`DatabricksSessionStoreSaver`), not to `WorkspaceClient.mason`. The Mason
surface in `databricks-sdk==0.146.0` exposes the same operations (listed
below) but is unused by this checkout.

### Release status, cost, retention, limits

| Fact | Status | Source |
|---|---|---|
| AgentKit search `limit` default / max | Default 100, max 100 | Installed `databricks_agentkit.memory_store.MemoryStore.search` and `_pagination._MAX_PAGE_SIZE = 100`. APX already clamps `recall` with `min(opts.k, 100)`. |
| Page size | 1–100 inclusive | Same `_pagination.py`. |
| Store-level grants | `grant_permission(principal_id, permission="WRITE")` for a **service principal application (client) id** | AgentKit `MemoryStore.grant_permission` / `SessionStore.grant_permission` docstrings. Not called on `fevm`. |
| Product release | **Beta**, not GA and not Public Preview | [Managed agent memory](https://docs.databricks.com/aws/en/agents/agent-memory/managed-memory) (AWS, Azure, and GCP pages), fetched 2026-10-13: "This feature is in Beta." [Release types](https://docs.databricks.com/aws/en/release-notes/release-types): Beta is a private preview, not for production, may be removed, and is billed. |
| Where it runs | Enabled workspaces; **not** on GCP, AWS GovCloud, or Azure Gov | Same memory page. |
| Retention / TTL | **Not stated** on the product page or the installed client. No retention field on the live store objects. | Memory page + `fevm` `get`. |
| Cost / DBU | **Billed as Lakebase**. Pricing page has no separate managed-memory SKU. | Memory page links to [Lakebase pricing](https://docs.databricks.com/aws/en/oltp/projects/pricing). |
| `fevm` feature availability | **Enabled.** List and get succeeded. A missing name returned `NOT_FOUND`, not `FEATURE_DISABLED`. | [Live probe](#live-probe-fevm-2026-10-13). |
| Live writes / cross-actor content | **Passed** on disposable store `apx-854-probe`, then deleted. APX `add` / `get` / `list` / `recall` / `update` / `delete` round-tripped for actor A. Actor B's APX `get`, `list`, and `recall` were empty. Direct service `list` / `search` for actor B were also empty — this service filters by `actor_id` on those calls. `get` by id has no actor argument; APX hides the row after the service returns it. | [Write probe](#write-probe-fevm-2026-10-13). |

### Installed AgentKit methods this checkout can call

Constructed as `AgentKitClient(workspace_client=ws)` with two collections:
`memory_stores` and `session_stores`. HTTP prefix used by the private
transport: `/api/2.0/agents/{memory-stores,session-stores,...}`.

**Memory store (workspace-scoped resource)**

| Method | Signature (installed 0.4.0) | Used by APX today |
|---|---|---|
| `MemoryStores.create` | `(display_name, *, description=None)` | `provision_managed_memory` |
| `MemoryStores.get` | `(store_id)` | `ManagedMemoryStore`, provision, doctor, deploy preflight |
| `MemoryStores.list` | `(*, page_size=None)` | not used |
| `MemoryStore.add` | `(*, actor_id, path, content, session_id=None, description=None, source_type=None, write_mode=None)` | `ManagedMemoryStore.add` (sets `actor_id`, `path=/memories/{ns}/{id}`, `content`, `description`; does not pass `session_id` / `source_type` / `write_mode`) |
| `MemoryStore.get` | `(memory_id, *, read_mask=None)` | `_owned_entry` — **not actor-scoped**; APX then compares `entry.actor_id` |
| `MemoryStore.list` | `(*, actor_id, path_prefix=None, session_id=None, page_size=None, read_mask=None)` | `ManagedMemoryStore.list` |
| `MemoryStore.search` | `(*, actor_id, query, limit=None, path_prefix=None, session_id=None, read_mask=None)` | `ManagedMemoryStore.recall` |
| `Memory.update` | `(*, content=None, description=None)` | `ManagedMemoryStore.update` (content + derived description only) |
| `Memory.delete` | `()` | `ManagedMemoryStore.delete` |
| `MemoryStore.grant_permission` | `(principal_id, *, permission="WRITE")` | mentioned in the provision status string; not called automatically |
| `MemoryStore.update` / `delete` (the store itself) | display_name / description; delete store | not used |

A Memory entry's identity in the SDK is `name` + `actor_id` + `path` +
optional `session_id`. There are **no** fields for tags, importance, embeddings,
or caller metadata. APX therefore maps those MemoryStore fields to empty /
default values (`tags=()`, `importance=0.5`, `metadata={}`, `embedding=None`).

**Session store (workspace-scoped resource)**

| Method | Signature | Used by APX today |
|---|---|---|
| `SessionStores.create / get / list` | name + optional description/metadata | deploy / doctor `session_stores.get`; Agent Bricks may create during deploy |
| `SessionStore.add` | `(*, actor_id, session_id=None, parent_session_id=None, metadata=None)` | via `DatabricksSessionStoreSaver` |
| `SessionStore.get / list / delete / grant_permission / update` | as named | saver + doctor/deploy get |
| `Session.append_items` | `(items: Sequence[Any]) -> list[SessionItem]` | saver `put` / `put_writes`. Items are **opaque JSON**. |
| `Session.list_items` | `(*, page_size=None, order_by=None)` | saver reads |
| `Session.pop_item` / `clear_items` | — | saver internals; not an APX ConversationStore |
| `Session.fork` | `(*, actor_id, up_to_item_id=None, session_id=None, metadata=None)` | not wrapped as ConversationStore (docs mention it as managed-only) |
| `Session.delete` | `(*, force=False)` | cascade requires `force=True` when descendants exist |
| `Session.extract_memories` | `(*, memory_store, instructions=None, dry_run=False)` | **not called**. Tracked by #858. |
| `DatabricksSessionStoreSaver` | LangGraph `BaseCheckpointSaver` over session items | native durable default |

Saver contract, quoted from the installed class docstring:

> Both `thread_id` and `actor_id` are required in the run config's
> `configurable` section. … the Session Store treats item `data` as opaque JSON.

`thread_config(session_id, actor=None)` in the same module documents actor as
a partition supplied by the caller and defaults it to `session_id` when omitted.
That default is **not** an authorization grant.

### Mason (present, unused)

`databricks.sdk.service.mason.MasonAPI` in SDK 0.146.0 exposes the matching
RPC names (`create_memory_store`, `search_memories`, `append_session_items`,
`extract_memories`, `fork_session`, …). APX does not call it. Switching
transports is out of scope for 854.

## Existing APX persistence contracts

There are four distinct persistence kinds. They are not interchangeable.

```
                    ┌─────────────────────────────────────┐
 completed turns    │ ConversationStore                   │  UI listing, search,
 session_state      │  inmemory | lakebase                │  temp: stripped
                    └─────────────────────────────────────┘
                                      ≠
                    ┌─────────────────────────────────────┐
 mid-turn graph     │ Checkpointer (LangGraph saver)      │  approvals, resume
 + messages in      │  InMemorySaver | Lakebase           │  native: managed
   graph state      │  PostgresSaver | DatabricksSession  │  Session Store
                    │  StoreSaver                         │
                    └─────────────────────────────────────┘
                                      ≠
                    ┌─────────────────────────────────────┐
 cross-session      │ MemoryStore                         │  facts / recall
 facts              │  inmemory | lakebase | managed      │
                    └─────────────────────────────────────┘
                                      ≠
                    ┌─────────────────────────────────────┐
 few-shot           │ ExampleStore                        │  out of 854 scope
                    │  inmemory | lakebase                │  (no managed backend)
                    └─────────────────────────────────────┘
```

### Transcript ownership (#331)

Closed #331 decided, on the served path:

> checkpointer is the zero-config in-process tier; a configured
> ConversationStore is the durable tier and keeps its replay path (no
> checkpointer injected → no double-persist, no double-context).

That decision still holds for the Responses / ChatAgent path. Native durable
agents invert the default: a managed Session Store **is** the checkpointer
(`DatabricksSessionStoreSaver`), and `_build_conversation_store` still does
not know `type="managed"`. So today:

- Responses / chat + `session.type = lakebase` → ConversationStore owns the
  durable transcript; Lakebase `PostgresSaver` owns mid-turn checkpoints
  (approvals).
- Native durable (default) → managed Session Store owns **graph checkpoints**.
  That is not the same as owning the ConversationStore transcript
  (list / search / `session_state` / UI history).
- A persisted transcript is **not** proof of a durable execution checkpoint.
  A managed checkpoint item is **not** proof of UI listing, full-text search,
  or `temp:`-stripped session state.

Do not collapse these. #857 tracks transcript migration onto managed
sessions; #864 tracks live approval resume; #859 tracks retirement.

## Operation-by-operation map

### 1. MemoryStore

Protocol: [`_memory.py` `MemoryStore`](../../python/src/apx_agent/_memory.py)
— `add`, `add_batch`, `get`, `update`, `delete`, `list`, `recall`.

Selection: `[tool.apx.agent.memory] type = inmemory | lakebase | managed`.
Wiring: `_memory_wiring._build_memory_store`. Native agents do **not**
auto-enable memory; it remains an explicit opt-in
([runtime-targets](../running/runtime-targets.md)).

| APX operation | inmemory | lakebase (pgvector) | managed AgentKit | Gap |
|---|---|---|---|---|
| `add(principal_id, content, namespace, tags, importance, metadata)` | full | full + embedding | `actor_id=principal_id`, `path=/memories/{ns}/{id}`, `content`, `description=first line`. tags / importance / metadata **dropped**. | metadata loss is documented in `_memory_managed.py` module docstring. |
| `add_batch` | yes | yes | sequential `add` | no native batch. |
| `get(memory_id)` | by id | by id | `get(id)` then **ownership check** against trusted scope | GET-by-ID is not actor-scoped in the service. Missing trusted principal → `None`. |
| `update` / `delete` | by id | by id | same ownership gate; update writes `content` + `description` only | cannot patch tags / importance / metadata. |
| `list(principal_id, namespace, tags, min_importance, limit)` | all filters | all filters | `list(actor_id, path_prefix=/memories/{ns}/)` then client-side `actor_id` re-check | tags / min_importance ignored. |
| `recall(query, k, …)` | cosine if embedder else recency | pgvector | BM25 `search`, `limit=min(k, 100)` | no vector recall; no tag/importance filter; no search pagination (SDK returns a list, not a page token). |
| store name | n/a | table name | workspace display name `[a-z][a-z0-9-]{1,54}[a-z0-9]` | catalog.schema.name rejected. Historical UC rows need explicit export/import (#859). |

Trusted principal:

- `add` / `list` / `recall` require a non-empty `principal_id` and raise
  `ValueError("Managed memory requires a trusted principal_id")`.
- `get` / `update` / `delete` use `scope_resolver` (wired to
  `current_principal()` from `_memory_tools`, falling back to the local CLI
  identity). They compare `entry.actor_id == actor` before exposing or
  mutating.
- Actor matching is **application-side**. Store permissions remain the
  security boundary — quoted from the module:

  > Actor IDs partition memories; store permissions are the security
  > boundary.

### 2. ConversationStore (transcript + UI + session_state)

ABC: [`_conversation.py` `ConversationStore`](../../python/src/apx_agent/_conversation.py).
Backends: `InMemoryConversationStore`, `LakebaseConversationStore`. There is
**no** `ManagedConversationStore`.

Selection: `[tool.apx.agent.session] type = inmemory | lakebase` for the
served Chat/Responses path. `type = managed` is legal **only** on
`target = "durable_agent_server"` and does **not** enter
`_build_conversation_store` — that function still raises
`unknown type 'managed'. Known: inmemory, lakebase.` Native managed sessions
bind as a checkpointer instead.

| APX operation | inmemory / lakebase | managed Session Store (installed SDK) | Status |
|---|---|---|---|
| `create_conversation(id, kind, title, parent, agent_id)` | yes | `SessionStore.add(actor_id, session_id, parent_session_id, metadata)` | **unmapped**. No `kind` / `title` / `agent_id` fields. `title` would have to live in `metadata` (string map) if at all. |
| `get_conversation` | yes | `SessionStore.get(session_id)` | **unmapped** as ConversationStore. Saver uses it internally. |
| `list_items` (cursor `after`/`before`, `type` filter) | yes | `Session.list_items(page_size, order_by)` — opaque `data`, page_size ≤ 100 | **partial**. No item-type filter; APX cursors are item ids, SDK uses page tokens. |
| `append` typed conversation items | yes | `Session.append_items(items)` opaque JSON | **unmapped** for the served transcript. Saver writes checkpoint/channel/writes events, not ChatAgent messages. |
| `list_conversations` (kind, agent_id, title search) | yes | `SessionStore.list(filter, order_by)` | **unmapped**. Filter language is SDK-specific, not APX `kind`/`agent_id`. |
| `search` full-text over items | yes | **no session-item search API** | **gap**. Memory `search` is BM25 over memories, not transcripts. |
| `update_conversation` (title, reasoning_effort, model_override) | yes | `Session.update(metadata=…)` string map only | **gap** for typed fields. |
| `set_session_state` JSON dict, `temp:` stripped | yes | no dedicated field. Could be one opaque item or `metadata` (strings only). | **gap**. Mid-turn `state` already lives in the checkpointer channel; that is not `Conversation.session_state`. |
| `delete_conversation` | yes | `Session.delete(force=False)` | **unmapped**. Descendants require `force=True`. |
| fork | stripped from APX ABC | `Session.fork(...)` | managed-only; do not add a UI feature just because the API exists (#857). |

`_conversation.py` still lists fork-related methods among the agent-plane
methods **stripped** from the omniagents copy. That is intentional.

### 3. Session-scoped temporary state

[`_session_state.py`](../../python/src/apx_agent/_session_state.py) +
[`docs/design/session-state-persistence.md`](session-state-persistence.md):

- Seed `graph.state` from `Conversation.session_state` on the served
  Chat/Responses adapters.
- Persist `persistable_state(final)` through
  `ConversationStore.set_session_state`.
- Keys starting `temp:` are scratch and never written. `user:` / `app:`
  are also excluded from `set_session_state` (#947).
- Persist is never fatal.

On the native durable path the graph checkpointer already stores channel
values, including `state`. That is **not** the same write surface: it is
tied to `thread_id` / `actor_id` in the saver, has no `temp:` strip, and is
invisible to ConversationStore / Dev UI listing. Treat this as a gap in
#857, not as solved by the saver.

### 4. Graph checkpoints and approval resume

| Path | Implementation | What it persists | Approval resume |
|---|---|---|---|
| Served default | LangGraph `InMemorySaver` | process-local graph state | dies on restart / other replica |
| Served + `session.type=lakebase` | `build_lakebase_checkpointer` → `PostgresSaver` | checkpoints in Lakebase; ConversationStore still holds completed turns | survives restart **if** the saver built. Build failure degrades to in-memory with a warning (`_memory_wiring.resolve_checkpointer`). |
| Native durable default | `DatabricksSessionStoreSaver` on the derived Session Store | opaque session items (`checkpoint`, `channel_data`, `writes`) | intended; **live proof is #864**, not this issue. `compile_durable_handlers` raises `Approval resume requires a checkpointed session` when `resume` is set without a checkpointer. |
| Native + explicit `checkpointer=` | allowed only when no managed `session_store` | as provided | cannot combine with declared managed store. |

Comments in [`_checkpoint_lakebase.py`](../../python/src/apx_agent/_checkpoint_lakebase.py)
state the ownership split this map preserves:

> A pending mid-turn approval lives entirely in checkpoint state (not in the
> ConversationStore, which only persists *completed* turns).

Native recovery (`recovery: true`) resumes invocation-tagged managed
checkpoints for **service-identity** LlmAgents only. Request-user recovery
is rejected.

### 5. Session-to-memory extraction

Installed: `Session.extract_memories(memory_store=, instructions=, dry_run=)`.
APX does not call it. #858 owns adoption. Do not treat ConversationStore
history or MemoryStore `add` as a substitute.

## Actor isolation versus authorization

These are two different checks. Actor naming is not a grant.

| Layer | What it does | What it is not |
|---|---|---|
| Store permission (`grant_permission` / workspace ACLs) | Allows a **principal** (typically the serving SP) to read/write the whole store. | Per-actor tenancy. |
| `actor_id` / `principal_id` | Partitions rows inside a store the caller can already reach. | Access control. Any principal with store access can pass another actor id to the service. |
| APX trusted-principal wiring | Derives actor from request identity (`current_principal`, Apps forwarded user, CLI identity fallback) and, for managed memory ID ops, refuses the row when `entry.actor_id` mismatches. | A service-side authorization decision. The comparison happens after the service has already returned the row. |
| Native request-user auth | SDK isolates invocation/session IDs by caller when request-user auth is required ([runtime-targets](../running/runtime-targets.md)). | App-auth-only agents keep shared app-scoped sessions. |

[`docs/running/sessions-and-memory.md`](../running/sessions-and-memory.md)
already states the product rule this issue must keep:

> `actor_id` partitions data; it is not access control. Managed stores are
> workspace-scoped and authorized at the store level: any principal that can
> reach a store can read and write *every* actor's entries. For strict
> isolation between tenants or users, use a separate store per boundary.

`docs/reference/configuration.md` currently says "User A's memories are
invisible to User B." That is true of the APX tool surface when the
principal is trusted; it is **false** as a description of the managed
service. Prefer the sessions-and-memory wording. Do not "fix" isolation by
trusting a client-supplied actor string.

## Doctor states

`check_agent_prerequisites` in [`_doctor.py`](../../python/src/apx_agent/_doctor.py):

| Situation | Status | Detail language |
|---|---|---|
| AgentKit APIs missing / incompatible (`AgentKitClient`, and for durable: `DurableAgentServer`, `InvocationAuthPolicy.user_required`, `DurableAgentServer.recover`, `DatabricksSessionStoreSaver`) | FAIL | "Required AgentKit APIs are missing or incompatible" |
| APIs present, no live client (`online=False`) | SKIP | "`{name}`: workspace availability **unverified** (offline or authentication unavailable)" |
| Client init failed | FAIL | "Workspace availability **unverified**: authentication/client initialization failed" |
| `FEATURE_DISABLED` / `NOT_IMPLEMENTED` / `UNIMPLEMENTED` | FAIL | "Required feature is disabled or **unavailable** in this workspace" |
| `PERMISSION_DENIED` / `UNAUTHENTICATED` | FAIL | "Access **denied**; feature enablement is **unverified**" |
| `NotFound` / `RESOURCE_DOES_NOT_EXIST` | FAIL, or WARN when `provision_stores` and the resource is a store | "Declared resource or API route was not found; feature enablement is **unverified**" |
| Other probe error | FAIL | "Workspace availability **unverified**: read-only probe failed" |
| `get(name)` returns a named resource | OK | "declared resource is readable with the checking identity; **runtime write access is not proven**" |

Doctor is read-only. It does not create stores unless a native deploy
provisioner is explicitly in play (#855). A green doctor check is not a
write/read-back proof.

## Python versus TypeScript

| Surface | Status in this checkout |
|---|---|
| Python `MemoryStore` / `ConversationStore` / Lakebase checkpointer / AgentKit adapter | present, as mapped above |
| Python native `DatabricksSessionStoreSaver` | present for `durable_agent_server` |
| `typescript/` tree | **absent**. `ls typescript` is "No such file or directory". Public docs say the APX TypeScript runtime and generated AppKit host are retired (`docs/running/runtime-targets.md`). |
| Speculative TypeScript managed-state client | **out of scope**. Do not invent one. Remaining TS support questions live on #869, not here. |

There is therefore no TypeScript column to fill in for 854. The matrix is
Python-only.

## Declaration rules that later issues must preserve

From [`_models.py`](../../python/src/apx_agent/_models.py)
`AgentConfig._runtime_contract` and `SessionBackendConfig`:

- Durable target with no `session` block → inject
  `SessionBackendConfig(type="managed")` and derive
  `store_name` from the agent name (`{name}-sessions` shape via
  `_managed_store_name`).
- Durable + explicit non-managed session → rejected ("Declared durable
  sessions require type='managed'; explicit checkpointers remain available
  through compile_agent").
- Non-durable + `session.type='managed'` → rejected.
- Managed session + Lakebase connection fields → rejected.
- Managed session `auto_create=true` in the declaration → rejected; runtime
  `auto_create` is forced false. Provisioning belongs to deploy.
- Managed memory `store_name` is a workspace display name; omitted names
  derive from the agent name. Legacy three-part names fail validation.

`_build_conversation_store` and `resolve_checkpointer` are still the
**Lakebase / in-memory** wiring. They are not the native managed binding.
Do not "fix" that in 854; #857 / #859 own the merge.

## What later issues take from this map

| Issue | Use this page for |
|---|---|
| #855 provision / declare | Display-name contract, doctor states, `grant_permission` is store-level and admin-time, `auto_create` stays off at runtime. |
| #856 live memory CRUD | Method table + dropped tags/importance/metadata + GET-by-ID ownership check. Do not replace the AgentKit adapter again to chase an older SDK name. |
| #857 managed transcripts | ConversationStore rows marked **unmapped** / **gap**. Do not treat the saver as a transcript store. Keep #331's no-double-persist rule. |
| #858 extraction | `Session.extract_memories` exists in 0.4.0 and is unused. |
| #859 migrate / retire | UC name rejection, Lakebase still required for vector recall / transcript search / `session_state` / examples. Inventory consumers before deleting `_conversation*`, `_session_state.py`, `_checkpoint_lakebase.py`. |
| #864 execution proof | Approval resume + restart/replica continuity are **not** closed by this document. |
| #869 TypeScript | No client in this tree; do not start one from 854. |

## Live probe: `fevm`, 2026-10-13

Profile `fevm` (`~/.databrickscfg`, `auth_type = databricks-cli`) against
workspace `fevm-serverless-stable-7def3z`. Identity:
`stuart.gano@databricks.com`. Packages: `databricks-sdk` 0.146.0,
`databricks-agentbricks` 0.4.0. Read-only. No store was created, no memory
or session item was written, and no entry content was printed.

| Call | Result |
|---|---|
| `current_user.me` | OK |
| `AgentKitClient(workspace_client=ws)` | Constructed |
| `memory_stores.list` | 1 store: `van-bsmmyz-memory` (created 2026-10-07) |
| `session_stores.list` | 1 store: `van-bsmmyz-sessions` (created 2026-10-07) |
| `memory_stores.get("van-bsmmyz-memory")` | OK. Owner is the calling user. `storage_backend.backend_type = STORAGE_BACKEND_TYPE_LAKEBASE`. No retention or state field. |
| `session_stores.get("van-bsmmyz-sessions")` | OK. `metadata = {}`. No retention field. |
| `WorkspaceClient.mason.list_memory_stores` | Same one store. Mason lists; APX does not call it. |
| `memory.list` / `memory.search` for the signed-in user | OK, 0 entries |
| Same calls with a sentinel actor id that is not a user | OK, 0 entries. The service accepts another actor key from a principal who can read the store. This does **not** prove it would return another actor's content; the store was empty. |
| `get` of a name that does not exist | `AgentCliError`, `error_code=NOT_FOUND`. Not `FEATURE_DISABLED`, not `PERMISSION_DENIED`. |

`AgentCliError` subclasses `click.ClickException`, not
`databricks.sdk.errors.NotFound`. `check_agent_prerequisites` only
special-cases SDK `NotFound` / `PermissionDenied` /
`FEATURE_DISABLED`. A missing store probed through AgentKit would therefore
be reported as "availability unverified," not as "resource was not found."
That is a doctor classification gap, not a workspace outage. Not fixed in
this issue.

### Write probe: `fevm`, 2026-10-13

Disposable store `apx-854-probe` only. `van-bsmmyz-*` was not used.
Entry content was a one-line disposable sentence. Store and entries were
deleted; workspace `memory_stores.list` then showed only
`van-bsmmyz-memory`.

| Step | Result |
|---|---|
| `memory_stores.create("apx-854-probe")` | OK. Resource `memory-stores/apx-854-probe`. |
| APX `add` for actor `apx-854-actor-a`, namespace `probe` | OK. APX id is the entry UUID (`16cb0b33-…`). Tags / importance / metadata stayed at the adapter defaults (`()`, `0.5`, `{}`). |
| APX `get` as actor A | OK. Content matched. |
| APX `get` as actor B (`scope_resolver` only) | `None`. Service `get` has no actor argument; `_owned_entry` hid the row. |
| APX `list` / `recall` as A | 1 row. BM25 `recall` score ≈ 2.86. |
| APX `list` / `recall` as B | 0 rows. |
| Direct `MemoryStore.list` / `search` as B | 0 rows. The service itself filters list/search by `actor_id`. Isolation on those calls is not APX-only. |
| APX `update` then `delete` as A | Update matched. Delete returned true. Subsequent `get` was `None`. |
| `store.delete()` | OK after the last entry was gone. Confirmed `NOT_FOUND`. |

Practical notes from the same session:

- Deleting a store that still has entries fails with "the store already
  has memories." The client has no force flag.
- `Memory.name` is `memory-stores/{store}/entries/{uuid}`. APX stores
  only the UUID. `ManagedMemoryStore.get` accepts that UUID. Passing the
  full resource name to `MemoryStore.get` 404s.
- `grant_permission` was not called. Retention and measured DBU remain
  unknown. Doctor was not run against a project; the API results above
  substitute for that read-only check.

### Provision / CRUD probe: `DEFAULT`, 2026-10-21

Profile `DEFAULT` on workspace `fevm-serverless-stable-7def3z`. Disposable
stores `apx-862-855-probe` and `apx-862-855-sessions` only. Both were
deleted afterward. Re-run with `APX_MANAGED_STATE_LIVE=1 pytest
python/tests/test_managed_state_live_reality_ctk.py`.

| Step | Result |
|---|---|
| `provision_managed_memory` first call | Created `apx-862-855-probe`. |
| `provision_managed_memory` second call | Idempotent: already exists. |
| `session_stores.create` then `get` | Created and readable. |
| APX `add` / `get` / `list` / `recall` / `update` / `delete` for actor A | All OK. Isolation vs actor B held for get, list, and recall. |
| `memory_stores.get` of a missing name | `NOT_FOUND`. |
| Store `delete` after emptying entries | OK for memory and session stores. |

`grant_permission`, retention, and measured DBU were not part of this probe.
No App deploy and no idle/wake run.

## Sources

- This checkout: `_memory.py`, `_memory_managed.py`, `_memory_wiring.py`,
  `_memory_tools.py`, `_conversation.py`, `_conversation_lakebase.py`,
  `_session_state.py`, `_checkpoint_lakebase.py`, `_durable_agent.py`,
  `_models.py`, `_runtime_targets.py`, `_doctor.py`.
- Installed `databricks_agentkit` 0.4.0 (from `databricks-agentbricks`)
  and `databricks.sdk.service.mason` 0.146.0.
- Docs: [sessions-and-memory](../running/sessions-and-memory.md),
  [runtime-targets](../running/runtime-targets.md),
  [session-state-persistence](session-state-persistence.md),
  [configuration](../reference/configuration.md).
- Issues: #854, #855–#859, #331 (closed), #862 parent, #864, #869.
- Public pages fetched 2026-10-13:
  [Managed agent memory](https://docs.databricks.com/aws/en/agents/agent-memory/managed-memory)
  (Beta; Lakebase-backed; not on GCP or Gov),
  [release types](https://docs.databricks.com/aws/en/release-notes/release-types)
  (Beta = private preview, billed, not for production),
  [Lakebase pricing](https://docs.databricks.com/aws/en/oltp/projects/pricing).
- Live read-only probe: profile `fevm`, workspace
  `fevm-serverless-stable-7def3z`, 2026-10-13.
- Historical issue source:
  <https://databricks-sdk-py.readthedocs.io/en/stable/workspace/mason/mason.html>.
