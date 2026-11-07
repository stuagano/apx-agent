# Design: typed memory strategies (`facts`, `preferences`, `summary`)

**Status:** accepted 2026-11-06 (owner approved the recommendations below) · **Date:** 2026-11-06 ·
**Issue:** [#1088](https://github.com/stuagano/apx-agent/issues/1088) ·
**Blocks:** [#1089](https://github.com/stuagano/apx-agent/issues/1089) (when extraction runs) ·
**Interacts with:** [#1087](https://github.com/stuagano/apx-agent/issues/1087) auto-recall (shipped in #1094),
[#1090](https://github.com/stuagano/apx-agent/issues/1090) retention

This note decides **what** gets extracted from a session into long-term memory.
#1089 decides **when** extraction runs. No code ships with this note.

## Today on main

- `memory=` is a `MemoryBackendConfig` (`python/src/apx_agent/_models.py`). Namespaces
  are free-form strings. The memory tools and auto-recall default to
  `namespace_default` (`"default"`).
- Managed memory stores an entry at `/memories/<namespace>/<id>` with the caller as
  `actor_id` (`_memory_managed.py`). Lakebase stores a `namespace` column.
- Session-to-memory extraction only exists for managed memory:
  `memory.extraction = {enabled, instructions}`, run by `apx-agent memory extract`
  (`_memory_extract.py`). It calls AgentKit `Session.extract_memories(memory_store=,
  instructions=, dry_run=)`, which takes **one** instructions string and lets the
  service choose each entry's `path`. apx then reads every write back. The
  validator refuses `extraction` on in-memory and Lakebase stores.
- `consolidate_memories` summarizes memories that **already exist** into a
  `consolidated` namespace. It is not session extraction, and this note leaves it alone.
- Auto-recall (#1094) recalls the top `recall_k` memories from `namespace_default` each turn.

## Prior art (primary sources)

- AgentCore defines built-in strategies: semantic, user preference, summary and
  episodic ([built-in strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/built-in-strategies.html)).
  Each step's system prompt is made of "Instructions" plus an "Output schema".
- With "built-in with overrides" you can override "the instructions in the system
  prompt (however, the output schema remains the same)" and the model. AgentCore
  assumes a declared `memoryExecutionRoleArn` to call that model in your account
  ([custom strategy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-custom-strategy.html)).
- Summaries are per session. Their default namespace is
  `/strategy/{memoryStrategyId}/actor/{actorId}/session/{sessionId}/`
  ([summary strategy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/summary-strategy.html)).
- Episodic: "Because reflections can span multiple actors within the same memory
  resource, consider the privacy implications of cross-actor analysis"
  ([episodic strategy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/episodic-memory-strategy.html)).
- Strands does not run extraction itself. Its AgentCore integrations send turns, and
  "the memory resource's strategies decide which namespaces receive extracted records."
  Recall is configured per namespace (`top_k`, `relevance_score`)
  ([AgentCore memory store](https://strandsagents.com/docs/integrations/memory-stores/agentcore-memory-store/),
  [session manager](https://strandsagents.com/docs/integrations/session-managers/agentcore-memory/)).
  Both pages say they are community-maintained.

## Decisions

### 1. Strategy names → namespaces

| Strategy | Extracts | Namespace | Records per session |
|---|---|---|---|
| `facts` | stated facts about the user and their work | `facts` | 0..n |
| `preferences` | how the user wants things done | `preferences` | 0..n |
| `summary` | a recap of one session | `summary` | exactly 1, upserted |

**Decision:** the strategy name is the namespace. There is no template language, so
`{actorId}` stays the existing principal partition and is never a path segment users
write. Summary records carry `session_id`: the managed entry field, or `metadata`
on Lakebase. Re-extracting a session replaces its summary and does not add a
second one. We use `facts` rather than AgentCore's "semantic" because it says what
the records hold.

### 2. Declaration shape

```python
memory={
    "type": "lakebase",                       # or "managed" / "inmemory"
    "strategies": ["facts", "preferences", "summary"],
    "strategy_prompts": {"preferences": "Only capture reporting-format preferences."},
}
```

- `strategies: list[Literal["facts", "preferences", "summary"]] | None`. Absent means
  no strategies, and today's behavior is unchanged byte for byte.
- `strategy_prompts: dict[strategy, str]`. Each key must be a declared strategy, and
  blank text is refused.
- Rejected option: a per-strategy object (`{"name", "prompt", "retention"}`). It
  duplicates #1090's per-namespace retention, and no test needs it yet.

### 3. Extraction prompt: prompts can be overridden, the schema cannot

Each strategy has a built-in instructions string in code. `strategy_prompts[s]`
**replaces** that string. apx always appends the strategy's fixed output contract,
and no declaration can change it:

- `facts` / `preferences`: a JSON array of strings, one memory per string, `[]` when
  there is nothing worth saving;
- `summary`: one JSON string.

Output that fails to parse raises an error and writes nothing. It is not silently
dropped. This matches AgentCore's rule: instructions can be overridden, the output
schema cannot.

### 4. Lakebase (and in-memory) extractor: model and identity

**Options.** (a) The agent's own `model`, as #1088 proposes. (b) A declared
`extraction_model`, like AgentCore's override model.

**Recommendation: (a), with no new field.** `memory=` is accepted only on
`LlmAgent`, which already names a serving endpoint. A separate model can be added
when a real agent needs one.

**Identity.** Extraction runs off the request path (#1089 triggers it after N turns
or when a session goes idle), so no OBO user token exists. It runs as the **app
service principal**, the same `WorkspaceClient` that builds the Lakebase engine and
the embedding function today. Every record is written with `principal_id` set to
the **session's recorded user**, never to the service principal. `doctor` reports
whether the service principal can query the model endpoint. The extractor is
written against the `MemoryStore` protocol, so in-memory works too. The
"`extraction` requires managed" validator gets relaxed for `strategies`.

### 5. Managed backend: strategies feed the existing extraction instructions

The service picks entry paths and accepts one `instructions` string, so a single
call cannot guarantee the namespace mapping.

| Option | Namespace mapping | Cost |
|---|---|---|
| M1: one call, all strategy prompts concatenated | not guaranteed (the service picks paths) | 1 call |
| **M2: one `dry_run=True` call per strategy, then apx `add()` at `/memories/<strategy>/<id>`** | exact | N calls (N ≤ 3) |

**Recommendation: M2.** The prompt for each call is `extraction.instructions` (if
declared) followed by the strategy prompt. The existing per-entry read-back still
applies to apx's own writes. `ManagedMemoryStore.add` has to forward `session_id`,
which the AgentKit `add` already accepts. Without `strategies`, the current
single-call path is unchanged.

### 6. Recall (#1087) and retention (#1090)

- **Auto-recall:** with strategies declared, recall covers `facts`, `preferences`
  and `namespace_default`, merges the results by score within the one store, and
  keeps the top `recall_k` above `recall_min_score`. Scores are compared only within
  a store (BM25 for managed, cosine for Lakebase). `summary` is **not** recalled by
  default: summaries match the latest user message poorly and lengthen the prompt.
  An agent opts in by listing it in `recall_namespaces`. Prefer one store query over
  a namespace list where the store supports it; otherwise one query per namespace
  (at most 3 by default). `consolidated` is not recalled unless it is listed.
- **Retention:** #1090 keys retention by namespace. Strategies are namespaces, so
  `{"summary": ...}` covers them and no strategy-specific field is added. Managed
  stores keep #1090's position: the platform does not state a retention period, so
  apx reports that it has none.

### 7. Why episodic is out

Episodes need completion detection and a structured XML schema, and AgentCore's
reflections can aggregate across actors. That conflicts with the per-actor
partition that apx memory relies on. No consumer has asked for it. Revisit it only
for reflections scoped to a single actor.

## Test plan (for the implementation PR)

1. **Config:** unknown or `"episodic"` strategy names are refused with a message
   that names the allowed set. `strategy_prompts` keys outside `strategies` are
   refused, and so is blank text. No field changes the output contract.
2. **Extractor (in-memory store, fake model):** records land in the namespace for
   their strategy, with `principal_id` set to the session user and not the service
   principal. Re-running a session leaves one `summary`. Malformed model output
   raises and writes nothing.
3. **Managed (fake AgentKit):** one dry-run call per strategy with the composed
   instructions, then writes at `/memories/<strategy>/`. A read-back mismatch
   raises. With no `strategies`, the call sequence matches today's.
4. **Auto-recall:** the middleware recalls `facts`, `preferences` and the default
   (not `summary`), merges them, and caps the result at `recall_k`. Listing
   `summary` in `recall_namespaces` adds it.
5. **`*_reality_ctk.py`:** compile an agent with strategies, extract one fixture
   session, then read each namespace back with `store.list` (the read is non-empty
   and contains the expected content), plus a `ctk.verify` of the recalled prompt block.
6. A docs-claims test pins the `sessions-and-memory.md` example to the real config.

## Resolved questions

- **Managed memory: M2.** One dry-run extraction call per strategy (at most 3) in
  exchange for an exact namespace. Extraction runs off the request path every few
  turns, so the extra calls are cheap, and recall and retention both depend on the
  namespace being right.
- **Lakebase extractor model: the agent's own model only.** No `extraction_model`
  field until a real agent needs one.
- **`summary` auto-recall: opt-in,** via `recall_namespaces` (see decision 6).
- **Transcript source for the Lakebase extractor:** left to #1089.
