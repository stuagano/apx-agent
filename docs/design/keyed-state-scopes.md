# Design: `user:` / `app:` keyed-state scopes (G3 phase 3)

**Status:** proposed · **Date:** 2026-06-25 · **Source:** ADK gap audit (G3, #232) ·
**Builds on:** [keyed-shared-state.md](keyed-shared-state.md) (phases 1–3 plan),
[keyed-state-tool-access.md](keyed-state-tool-access.md) (tool read/write),
[session-state-persistence.md](session-state-persistence.md) (phase 2 seam)

The last undesigned slice of G3. Phases 1 + the tool-access increment shipped the
in-graph `state` channel (`output_key`, `{key}` templating, `Dependencies.State`
tool read/write). Phase 2 designed session-scoped persistence. This phase adds the
remaining two ADK scopes — **`user:`** (this principal, across their sessions) and
**`app:`** (every user of this agent) — so a value can outlive a single
conversation and, for `app:`, be shared across users.

---

## Reality check — what is actually wired today (read before designing on top)

A read-back of the code (not the doc statuses) shows the G3 surface is **partly
dead-coded**, and phase 3 cannot stand on an unwired phase 2:

| Piece | State in code | Ref |
|---|---|---|
| `ApxState` channel + `_merge_state` reducer | **live** | `_compile.py:815,837` |
| `output_key`, `{key}` templating | **live** | `_compile.py:815,848`, `_agents.py` |
| `Dependencies.State` tool read/write (`StateProxy`) | **live** | `_state_proxy.py`, `_state_tool.py` |
| `ConversationStore.set_session_state` (ABC + 3 backends) | **live** | `_conversation*.py` |
| `persistable_state` / `persist_session_state` helpers | **live but unreferenced** | `_session_state.py` |
| Adapter **seed** (`graph.invoke({"messages", "state"})`) | **NOT wired** | `_chat_agent.py:623` invokes with `messages` only |
| Adapter **persist** (`persist_session_state(...)` after the turn) | **NOT wired** | only callers are `tests/test_session_state_store.py` |

So phase 2's *store mutator and helper exist and are unit-tested, but nothing seeds
`conv.session_state` into the graph and nothing calls `persist_session_state`.*
Session state never actually round-trips across turns yet.

**Prerequisite for phase 3:** wire the phase-2 seam first (seed at
`_chat_agent.py:623` / the `_responses_agent.py` invoke, persist after the turn in
both adapters), exactly as `session-state-persistence.md` §2–§3 specify. Phase 3
extends *that same seed/persist boundary*; building it on top of dead code would
ship two layers that have never executed end-to-end. This doc assumes the phase-2
wiring lands first (or as phase 3's opening commit).

---

## The target surface (ADK parity)

| ADK | apx phase-3 form |
|---|---|
| `State.USER_PREFIX` → `user:` | `state["user:pref_lang"] = "en"` — survives across this principal's conversations |
| `State.APP_PREFIX` → `app:` | `state["app:rollout_flag"] = "on"` — shared by every user of the agent |
| `State.TEMP_PREFIX` → `temp:` | already shipped (phase 2): in-graph only, never persisted |
| *(no prefix)* → session | already designed (phase 2): one conversation |

The prefix **is** the scope discriminator. In-graph, scoped keys keep their prefix
in the single `state` channel (no second channel, no reducer change) — `user:x` is
just a namespaced key. The prefix is interpreted only at the **persistence
boundary**: it routes the key to a store on the way out and re-prefixes it on the
way in.

```python
def remember_pref(lang: str, state: Dependencies.State) -> str:
    """Record the user's language for future sessions."""
    state["user:pref_lang"] = lang        # → user-scoped store on persist
    return f"will reply in {lang} from now on"
```

A later turn (even in a different conversation, same principal) seeds `user:pref_lang`
back into `state`, so `{user:pref_lang}` templating and `state["user:pref_lang"]`
reads observe it.

---

## Design

### 1. Identity — the owner key for each scope

Both ids are resolved **before** `compile_to_langgraph`, synchronously and cheaply,
so they are available at the seed point:

- **`user:` owner = principal id.** `_resolve_ws_and_headers(custom_inputs)`
  (`_chat_agent.py:610`; `extract_obo_headers` on the apps path) yields
  `headers.user_id` / `headers.user_email`. Use `user_id`, falling back to
  `user_email`. **No principal ⇒ `user:` is unavailable** (sessionless / no OBO):
  reads seed nothing, writes are skipped + logged. Never fatal — same posture as
  the phase-2 "degraded to sessionless" path.
- **`app:` owner = `agent_id`.** Bound at `ApxChatAgent.__init__`
  (`_chat_agent.py:468`,
  `self._agent_id = agent_id if agent_id is not None else getattr(inner, "name", None)`),
  immutable per deployment. No `agent_id` ⇒ `app:` unavailable, same degrade.

These are the only two new owner dimensions; the reducer, the graph, and the tool
surface are untouched.

### 2. The backing store — extend `ConversationStore`, don't introduce a new one

**Ponytail:** the agent already holds a configured `ConversationStore` (constructed
+ threaded through both adapters, with in-memory / Delta / Lakebase backends). Add
the scoped surface there rather than standing up, configuring, and wiring a second
store. Two methods + one small table:

```python
@abstractmethod
def get_scoped_state(self, scope: str, owner_id: str) -> dict[str, Any]:
    """Read the keyed state for (scope, owner). Empty dict when absent.
    scope ∈ {"user", "app"}; owner_id is the principal id or agent_id."""

@abstractmethod
def set_scoped_state(self, scope: str, owner_id: str, delta: dict[str, Any]) -> None:
    """Merge `delta` into the (scope, owner) state, per-key last-write-wins.
    NOT a full replace — see §4 on concurrency."""
```

Backends mirror `set_session_state`:
- **In-memory** (`_conversation.py`) — a `dict[(scope, owner_id), dict]`, merged
  under the lock.
- **Delta** (`_conversation_delta.py`) — one table `{prefix}_scoped_state(scope
  STRING, owner_id STRING, state STRING /*JSON*/, updated_at TIMESTAMP)`,
  PK `(scope, owner_id)`; `set` = read-merge-write (§4).
- **Lakebase** (`_conversation_lakebase.py`) — same shape via SQLAlchemy.

This reuses the `Conversation`/`Memory` storage idioms (`_memory.py` already scopes
rows by `principal_id`) without inheriting the semantic/vector machinery — scoped
state is a flat JSON KV, like `session_state`, not recall.

### 3. Seed (read) — partition by scope at turn start

At the phase-2 seed point, build the initial channel by merging three sources,
each re-prefixed:

```python
seed: dict[str, Any] = dict(conv.session_state)                 # no-prefix (session)
if user_id:
    seed |= {f"user:{k}": v for k, v in store.get_scoped_state("user", user_id).items()}
if agent_id:
    seed |= {f"app:{k}": v for k, v in store.get_scoped_state("app", agent_id).items()}
graph.invoke({"messages": lc_input, "state": seed})
```

`temp:` is never seeded (it is scratch). Unknown identity ⇒ that scope is simply
absent from `seed`.

### 4. Persist (write) — dispatch by prefix, **merge** for cross-session scopes

Generalize `persist_session_state` into a prefix-router applied to the final
`state`:

```python
buckets = {"session": {}, "user": {}, "app": {}}   # temp: dropped
for k, v in (final_state or {}).items():
    if k.startswith("temp:"):  continue
    if k.startswith("user:"):  buckets["user"][k[5:]] = v
    elif k.startswith("app:"): buckets["app"][k[4:]] = v
    else:                      buckets["session"][k] = v

store.set_session_state(conv_id, buckets["session"])        # full overwrite (phase 2)
if user_id and buckets["user"]:  store.set_scoped_state("user", user_id, buckets["user"])
if agent_id and buckets["app"]:  store.set_scoped_state("app", agent_id, buckets["app"])
```

**Why session is a full overwrite but `user:`/`app:` must be a per-key merge.**
Phase 2's "full overwrite is safe" argument relies on the owner (one conversation)
being effectively serialized — we seeded the whole prior state and the reducer laid
this turn's deltas on top, so `final` *is* prior+delta. That argument **breaks for
`user:`/`app:`**, whose owner spans concurrent sessions:

- Session A and session B both seed the same `app:` dict at their starts.
- A writes `app:x`, B writes `app:y`.
- If each *overwrote* the whole `app:` dict, the later writer would drop the other's
  new key. A full replace is a lost update.

So `set_scoped_state` does a store-side **read-merge-write**: `stored ⊕ delta`, delta
winning per key. A key another session added after our seed survives (it is in
`stored`, untouched by our delta); only concurrent writes to the *same* key collapse
to last-write-wins — acceptable and documented, same hazard class as the phase-1
reducer and the parallel-fan-in note.

**Consequence — deletion is not supported for scoped keys in phase 3.** Under
union-merge, `state.pop("user:x")` cannot reliably erase a stored key (merge has no
delete signal). Document it; a tombstone/delta protocol is YAGNI until a real case
needs it.

Persistence stays **never-fatal**: the response has already streamed; any backend
error (including a non-JSON-serializable value) logs and degrades, per phase 2.

### 5. Templating must learn the colon

`_TEMPLATE_KEY = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")` (`_compile.py:815`)
does **not** match `:`, so `{user:pref_lang}` is left literal today. Phase 3 widens
it to allow a single scope prefix:

```python
_TEMPLATE_KEY = re.compile(r"\{((?:user:|app:|temp:)?[a-zA-Z_][a-zA-Z0-9_]*)\}")
```

`_render_template` (`_compile.py:848`) already looks the captured key up in the
`state` dict, where scoped keys live prefixed — so the lookup is unchanged once the
regex captures the prefix. (Python `str.format` is *not* used — substitution is this
custom regex — so the colon is purely a regex concern, not a format-spec one.)

### 6. Governance — `app:` is a cross-tenant write

`user:` writes touch only the writing principal's own state — low blast radius.
`app:` writes mutate state **visible to every user of the agent** — a governed,
cross-user surface. Posture:

- Each scoped write logs one audit line (`scope`, `owner_id`, key count), matching
  the `set_session_state` governance stance — explicit + audited, no new audit
  table (YAGNI).
- **Open question (sign-off):** should tool-driven `app:` *writes* require explicit
  opt-in (e.g. an agent-level `allow_app_state_writes` flag), defaulting to
  read-only? An agent author may want `app:` as broadcast-read config seeded by an
  operator, not mutable by any user's tool call. Lean: ship `app:` read+write with
  audit in phase 3, add a read-only gate only if a real deployment needs it.

---

## Phasing within phase 3

1. **3a — wire phase 2** (prerequisite): seed + `persist_session_state` in both
   adapters. Makes session state actually round-trip; turns the existing helpers
   from dead code into the seam phase 3 extends.
2. **3b — `user:` scope:** `get/set_scoped_state` on the store (3 backends),
   identity = `user_id`, seed/persist routing, templating colon. Single-owner
   merge is the whole correctness story; no cross-user concern.
3. **3c — `app:` scope:** same store surface, owner = `agent_id`, plus the
   governance/audit line and the read-only-gate open question.

Recommend landing 3a + 3b first (the high-value, low-risk "remember me across my
sessions"), then 3c.

## Alternatives considered

- **A second, standalone scoped-state store** (its own ABC + config + wiring) —
  rejected: the agent already constructs and threads a `ConversationStore`; two
  methods + one table on it is far less surface than a parallel store to configure
  in every backend and adapter.
- **A second graph channel per scope** (`user_state`, `app_state`) — rejected:
  multiplies reducers and seed/persist plumbing for no author-visible gain; one
  prefixed `state` dict already expresses scope and keeps `Dependencies.State` /
  `output_key` / templating uniform across scopes.
- **Full-dict overwrite for `user:`/`app:` (reuse phase-2 persist verbatim)** —
  rejected: silently drops concurrent cross-session writes (§4). The per-key merge
  is the minimum that makes a cross-session owner correct.
- **Reuse the `MemoryStore`** (`_memory.py`, already `principal_id`-scoped) —
  rejected for the value path: memory is semantic/vector recall, not a flat keyed
  dict; the scoping *pattern* is the reuse, not the store.

## Risks / compatibility

- **Backward compatible:** graphs invoked without scoped seeds still work
  (`state` defaults `{}`); turns with no identity skip the scope; `result["messages"]`
  and message semantics unchanged.
- **JSON-serializable** scoped values (they persist to a JSON column); a `json.dumps`
  failure degrades the write, never crashes the turn — same as phase 2.
- **Concurrency:** documented per-key last-write-wins on a shared scoped key; no
  deletion of scoped keys (§4).
- **Upstream reconcile:** `get/set_scoped_state` join `set_session_state` on the
  mirrored `ConversationStore` ABC; note them in the module docstring's
  "stripped/added" list for the eventual `omniagents` import, as phase 2 did.

## Testing

- **Store unit:** `get/set_scoped_state` write→read-back on the in-memory store;
  the merge semantics (two deltas on disjoint keys both survive; same key →
  last-write-wins); Delta/Lakebase SQL construction.
- **Routing unit:** a final `state` with `temp:`/`user:`/`app:`/bare keys partitions
  into the right buckets with prefixes stripped; `temp:` dropped.
- **Identity-absent unit:** no `user_id` ⇒ `user:` reads seed nothing and writes
  no-op (logged, not raised); same for `app:`/`agent_id`.
- **Templating unit:** `{user:pref}` renders from a seeded `user:pref`; unknown
  scoped key left literal.
- **Round-trip integration (no LLM):** drive `_chat_agent` with a fake graph that
  emits a `user:` delta; assert seed merged the store's scoped dict (re-prefixed)
  and `set_scoped_state("user", uid, {...})` was called with the stripped key.
- **Cross-session merge integration:** two turns with different "concurrent" deltas
  on the same owner, disjoint keys → both persist (no lost update).
- **Degradation:** a `set_scoped_state` that raises does not fail the turn.
- **Reality/ctk:** after wiring 3a, assert the seam executes — a `*_reality_ctk`
  check that an actual turn seeds and persists (not just that the helper exists),
  closing the dead-code gap this doc surfaced.

## Out of scope

- TTLs / size caps / per-key retention policy on scoped state.
- Structured (non-text) `output_key` values into scoped keys (depends on
  `output_schema`, not yet in apx).
- Scoped-key deletion / tombstones (§4).
- Seeding/persisting on the bare `LangGraphExecutor` path (no conversation/identity
  handle; the served adapters remain the persistence surface).

## Open questions (for sign-off before 3a)

1. **Phase-2 wiring ownership:** land 3a (seed + persist) as the opening commit of
   this issue, or split it back to #240's follow-up? (Lean: open phase 3 with it —
   the scopes are meaningless until the boundary executes.)
2. **`app:` writability:** read+write+audit now, or read-only-by-default with an
   opt-in write flag? (Lean: write+audit; gate later if needed — §6.)
3. **Principal id choice:** `user_id` vs `user_email` as the owner key when both are
   present, and stability across workspaces. (Lean: `user_id`, fall back to email.)
4. **Scoped-key delete:** accept "no deletion" for phase 3, or design a tombstone
   now? (Lean: no deletion, document; revisit on demand.)
