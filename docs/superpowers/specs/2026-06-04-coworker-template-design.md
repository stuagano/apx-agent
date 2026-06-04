# Coworker Template — Design

**Status:** approved (brainstorm), pending implementation plan
**Date:** 2026-06-04

## Problem

The framework has one built-in template, `DataTemplate` (`name = "data"`), now
pre-grounded (PR #133). It is a competent *data assistant* but not a *coworker*:
it answers each session fresh. The thing that makes an agent a coworker — that it
**remembers you and the work over time** — exists only as low-level primitives
(three memory subsystems × three backends, wired by `attach_declared_memory`),
never bundled into a named, scaffoldable template.

The framework also has no `coworker` template at all — "coworker" appears only in
code comments and the GTM "connector → pre-grounded coworker" motion.

## Goal

A `coworker` template = a **pre-grounded data agent that remembers**, expressed as
one config block:
- builds on the `data` template (reuses #133 schema grounding),
- bundles **memory** (semantic facts + session continuity) behind a single tiered
  knob with an upgrade path,
- adds an optional **persona**,
- **never mandates lakebase** — works (persistently) without it, upgrades to it.

## Decisions (from brainstorm)

- `coworker` is a **new template** = `data` agent + memory + persona preset
  (not standalone/grounding-agnostic; not memory-as-a-bare-layer).
- Memory defaults to **`persistent` (delta / UC tables)** — a coworker that
  remembers from day one, no lakebase; degrades gracefully when infra is absent.
- The `memory` knob covers **semantic facts + session continuity**. The example
  *learning loop* (mining/consolidation) is **deferred** to a later opt-in.
- **`CoworkerAgent` is a first-class agent class** (subclass of `DataAgent`), and
  `CoworkerTemplate` wraps it — symmetric with `DataAgent` / `DataTemplate`.
  Coworker composes like any agent: direct use, as a `sub_agent`, or as a leaf in
  a `SequentialAgent` / `RouterAgent`. (Memory facts are just tools, so a
  constructor can wire them; session + the app `ws` are supplied by the framework
  at finalize/serve time — see §2.)
- Persona, when set, **replaces the instruction lead** ("You are {persona}.")
  ahead of the #133 schema grounding.

## Architecture

### 1. The `memory` knob (the upgrade ladder)

A single string value on the coworker template that normalizes into the existing
`MemoryBackendConfig` (`[tool.apx.agent.memory]`) and `SessionBackendConfig`
(`[tool.apx.agent.session]`), both set to the same backend tier:

| `memory =` | aliases | facts + session backend (`StoreType`) | infra needed | persistence |
|---|---|---|---|---|
| `"off"` | — | none (subsystems disabled) | none | stateless |
| `"inmemory"` | `local` | `inmemory` | none | ephemeral (lost on restart/replica) |
| **`"persistent"` (default)** | `delta` | `delta` (UC Delta tables) | SQL warehouse + UC write | survives restart |
| `"lakebase"` | — | `lakebase` (pgvector) | + Postgres (Lakebase) instance | production semantic |

Rules:
- **Default is `"persistent"`.** A scaffolded coworker remembers across restarts
  with no lakebase.
- **One knob, both subsystems.** `memory = "persistent"` sets facts + session to
  `delta`. (The example learning loop is not touched — deferred.)
- **Per-subsystem override wins.** If the user writes an explicit
  `[tool.apx.agent.memory]` and/or `[tool.apx.agent.session]` block, those take
  precedence over the knob for that subsystem (the knob only fills in subsystems
  the user did not configure).
- **`lakebase` needs its connection block.** Selecting `memory = "lakebase"`
  without the lakebase fields (`host`, `database`, `embedding_model`,
  `embedding_dim`) is a clear config error — lakebase is the one tier that needs
  explicit connection details. (`off`/`inmemory`/`persistent` need none.)
- **No lakebase is ever mandated** by the template or defaults.

### 2. `CoworkerAgent` class + `CoworkerTemplate` wrapper

New file `python/src/apx_agent/coworker.py`, mirroring `data_agent.py`
(`DataAgent` + `DataTemplate`).

```python
class CoworkerAgent(DataAgent):
    """A pre-grounded DataAgent that remembers — persona + memory (facts +
    session). Composes like any agent (sub_agent, SequentialAgent, ...)."""
    def __init__(self, catalog, schema, *, persona=None, memory="persistent",
                 warehouse_id=None, ws=None, genie_space=None, vector_index=None,
                 include_functions=True, name=None, extra_tools=None, **kwargs):
        ...

@template
class CoworkerTemplate:
    name = "coworker"
    title = "Coworker"
    description = "A pre-grounded data agent that remembers (facts + session); memory upgradeable off → inmemory → persistent → lakebase."

    class Spec(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        catalog: str
        schema_name: str = Field(alias="schema")     # 'schema' in config dicts
        warehouse_id: str | None = None
        persona: str | None = None
        memory: str = "persistent"                   # the knob (see §1)
        genie_space: str | None = None
        vector_index: str | None = None
        include_functions: bool = True

    def build(self, spec, *, ws=None) -> CoworkerAgent: ...   # just constructs CoworkerAgent
```

**`CoworkerAgent.__init__`:**
1. Calls `DataAgent.__init__` → pre-grounded SQL agent (reuses #133 auto-discovery;
   needs no `ws` to ground).
2. **Persona** woven into the instructions (see §3) — by passing a
   persona-aware `instructions` (or persona arg) down to the grounding builder.
3. **Memory knob → declared config.** The `memory` string is normalized (one
   shared helper, see below) into `MemoryBackendConfig` + `SessionBackendConfig`
   and stored on the agent as `self.memory_config` / `self.session_config`. It is
   **not** turned into live stores at construction — that needs the app `ws`,
   which the framework supplies at finalize/serve time.

**Why declared-config rather than eager stores:** memory-fact tools are built from
a store via `make_memory_tools(store=…)`, and a `delta`/`lakebase` store needs a
`ws`. The framework already builds those at the right moment —
`attach_declared_memory(agent, config, ws)` (finalize) and
`resolve_session_store(config, ws)` (serve lifespan) — with the app's `ws`. So
`CoworkerAgent` keeps #133's "no `ws` at boot" property by *declaring* its memory
intent and letting that existing path wire it.

**Framework reads the agent's declared config (small precedence addition):**
- `attach_declared_memory` and `resolve_session_store` currently read only
  `AgentConfig.memory` / `AgentConfig.session`. They gain one fallback:
  **explicit `AgentConfig` block > agent-carried `self.memory_config` /
  `self.session_config` > none.** So a `CoworkerAgent` dropped into any served app
  gets its memory wired by the framework with the proper `ws`, and an explicit
  pyproject `[tool.apx.agent.memory]` still overrides.
- (Optional convenience) when a `ws` *is* passed to the constructor, the agent may
  eagerly build the memory-fact tools itself and set the existing
  `_apx_memory_attached` sentinel so finalize doesn't double-attach. Default path
  is declare-and-defer.

**`CoworkerTemplate.build(spec, ws=)`** simply constructs and returns a
`CoworkerAgent(...)` from the Spec — the knob logic lives once, in the agent.

**Shared knob helper:** `normalize_memory_knob(value) -> (MemoryBackendConfig |
None, SessionBackendConfig | None)` in `coworker.py` maps
`off/inmemory/local/persistent/delta/lakebase` → the two config objects (or
`None`/`None` for `off`), validates the value, and errors on `lakebase` without
connection fields. Used by `CoworkerAgent.__init__`. Single source of truth.

### 3. Persona composition

`persona` is an optional short role string. The grounded instruction builder
(`build_instructions_from_schema`, #133) gains an optional `persona` argument:
- with persona: lead = `"You are {persona}."` then `"You work with the data in
  {fqn} and already know the schema below — query it directly…"` + schema + rules.
- without persona: the current data-assistant lead is unchanged.

Grounding (table/column listing, "do not SHOW TABLES", recovery + grounding
rules) is identical either way — persona only colors the role sentence.

### 4. Graceful degradation (the "don't mandate" guarantee)

`attach_declared_memory` already skips a `delta`/`lakebase` store when no workspace
client is available (degrade rather than crash). The coworker relies on this so a
`persistent`-default coworker still **runs** on a bare workspace — it just won't
remember until the warehouse/UC (or Postgres, for lakebase) is reachable.

Addition: **surface the degradation** instead of failing silently — when a
configured memory tier is skipped, log a clear warning and expose it in the
`/readyz` self-check (a `memory` capability entry: `ok` / `degraded: <reason>`),
consistent with the "never show the user nothing" principle. A degraded coworker
is healthy-but-not-remembering, and that is visible.

### 5. Scaffold + upgrade UX

`apx scaffold <name> --template coworker` emits a project whose
`[tool.apx.agent]` block is:
```toml
[tool.apx.agent]
name = "coworker"
catalog = "samples"
schema  = "tpch"
# persona = "a revenue analyst on the GTM team"
memory  = "persistent"   # remembers across restarts via UC tables (no Lakebase)

# Upgrade path — no Lakebase required by default:
#   memory = "off"        # stateless
#   memory = "inmemory"   # zero infra, forgets on restart
#   memory = "persistent" # (default) UC Delta tables
#   memory = "lakebase"   # production pgvector — then add the block below:
# [tool.apx.agent.memory]
#   type = "lakebase"
#   host = "..."; database = "..."
#   embedding_model = "databricks-bge-large-en"; embedding_dim = 1024
```
The existing `data` scaffold and templates are unchanged. (The `.apx/schema.json`
manifest from #133 is written for the coworker scaffold exactly as for `data`.)

## Data flow

```
apx scaffold --template coworker
   → [tool.apx.agent] name="coworker", memory="persistent" + .apx/schema.json
build: CoworkerTemplate.build → CoworkerAgent(catalog, schema, persona=, memory=, ws=)
       └ DataAgent grounding (#133) + persona instructions
       └ normalize_memory_knob("persistent") → self.memory_config/session_config = delta
finalize/serve (framework, with app ws):
       attach_declared_memory(agent, config, ws)  reads config.memory else agent.memory_config
       resolve_session_store(config, ws)          reads config.session else agent.session_config
                                          → delta facts + session stores
                                          → if no ws/UC: skip + log + /readyz "degraded"
runtime: agent answers grounded AND recalls facts/session from prior turns
```
(Code use: `CoworkerAgent("samples","tpch", persona=…, memory="persistent")` composes
anywhere; the same finalize/serve path wires its declared memory with the app `ws`.)

## Error handling / degradation

- `memory` knob with an unknown value → config error listing the valid rungs.
- `memory = "lakebase"` without connection fields → config error naming the
  missing fields.
- `persistent`/`lakebase` configured but infra unreachable at runtime → degrade
  (store skipped), log a warning, mark `/readyz` memory `degraded`. Agent still
  serves.
- Per-subsystem `[memory]`/`[session]` blocks always override the knob.

## Out of scope

- The example **learning loop** (`_example_*`, mining/consolidation) — deferred
  to a later opt-in knob value (e.g. a future `examples` rung).
- A `CoworkerAgent` constructor class (template/config-only for now).
- New memory backends or changes to existing store implementations.
- Multi-source/grounding-agnostic coworker (it builds on the `data` shape).
- Per-user (principal-scoped) vs coworker-scoped memory policy changes — uses the
  existing scoping (facts per principal, examples per agent) unchanged.

## Testing

- **`normalize_memory_knob`:** each rung (`off`/`inmemory`/`local`/`persistent`/
  `delta`/`lakebase`) → the correct `(MemoryBackendConfig.type,
  SessionBackendConfig.type)` (or `(None, None)` for `off`); unknown value errors
  listing the valid rungs; `lakebase` without connection fields errors naming the
  missing fields.
- **`CoworkerAgent` construction:** is a `DataAgent` (grounded instructions
  present via #133); carries `memory_config`/`session_config` matching the knob;
  with `persona` set the instructions lead with `"You are {persona}."`, without it
  the data-assistant lead. Composes as a `sub_agent` (smoke).
- **Agent-carried precedence:** `attach_declared_memory` / `resolve_session_store`
  use `agent.memory_config` / `agent.session_config` when the `AgentConfig` block
  is absent, and the explicit block overrides it when present.
- **Persona in `build_instructions_from_schema`:** `persona=` changes only the
  lead; the grounding block + "do not SHOW TABLES" + rules are unchanged.
- **Degradation:** `persistent` + no `ws` → agent builds/serves, memory store
  skipped, a warning logged, `/readyz` reports memory `degraded`.
- **Template registry:** `coworker` registers and resolves by name; `data` still
  resolves (no collision). `CoworkerTemplate.build` returns a `CoworkerAgent`.
- **Scaffold:** `--template coworker` emits `name = "coworker"`,
  `memory = "persistent"`, the commented ladder, and `.apx/schema.json`.

## Affected files

- `python/src/apx_agent/coworker.py` — new: `CoworkerAgent` (subclass of
  `DataAgent`), `CoworkerTemplate` (wraps it), and `normalize_memory_knob`.
- `python/src/apx_agent/_schema.py` — `build_instructions_from_schema` gains an
  optional `persona` argument.
- `python/src/apx_agent/_memory_wiring.py` — `attach_declared_memory` and
  `resolve_session_store` add the agent-carried fallback (explicit config block >
  `agent.memory_config` / `agent.session_config` > none).
- `python/src/apx_agent/_wiring.py` (and/or the `/readyz` builder) — surface
  skipped-memory degradation as a `memory` capability in `/readyz`.
- `python/src/apx_agent/cli.py` — `--template coworker` scaffold (config block +
  ladder comments; reuse the `.apx/schema.json` write).
- `python/src/apx_agent/__init__.py` — export `CoworkerAgent` (and
  `CoworkerTemplate` if other built-ins are exported), next to `DataAgent`.
- Tests: `tests/test_coworker.py` (new), `tests/test_schema.py`,
  `tests/test_cli.py`, and the `/readyz` test location for the degradation entry.
