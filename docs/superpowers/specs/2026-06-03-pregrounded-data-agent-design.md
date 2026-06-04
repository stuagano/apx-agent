# Pre-grounded DataAgent — Design

**Status:** approved (brainstorm), pending implementation plan
**Date:** 2026-06-03

## Problem

The scaffolded data-agent example does not demonstrate that it understands its
data. It is generated as `DataAgent("samples", "tpch", …)` **without `ws=`**, so:

1. **No introspection runs.** `introspect_schema` is only called when `ws` is
   passed (`data_agent.py::_build_data_tools_and_instructions`), so `tables = {}`.
2. **Instructions are generic and self-defeating.**
   `build_instructions_from_schema` (`_schema.py`) falls to its no-tables branch,
   producing *"You are a data assistant for samples.tpch. Your data includes:
   samples.tpch"* — no table or column names — and, in **all** branches, the
   line *"At the start of every session, call the SQL tool to confirm what
   tables and columns are available before answering questions."*

That line is why the agent's first action is `SHOW TABLES IN samples.tpch`: it
rediscovers the schema live every session instead of arriving pre-grounded. It
reads as "let me go figure out what's here," not "I know this data."

## Goal

The scaffolded data-agent example comes **pre-grounded** in its schema:

- **Behavior:** answers directly, referencing real column names, with no
  `SHOW TABLES` discovery step.
- **Presentation:** the chat landing visibly shows the tables + key columns the
  agent understands, before the user asks anything.

## Decisions (from brainstorm)

- Fix **both** behavior and presentation.
- Grounding is **baked at scaffold time** (the scaffold already connects to a
  workspace), not introspected at runtime — instant, offline-safe, git-versioned,
  no warehouse needed at boot. This is the agent-as-config story.
- The baked schema lives in a **sidecar manifest** `.apx/schema.json` (structured
  data, serves both the instruction builder and the landing card; avoids
  pyproject TOML bloat).
- The served DataAgent **auto-discovers** the manifest from the app root, so
  `agent.py` stays the clean one-liner — grounding is pure config.

## Architecture

Five components, one cohesive feature.

### 1. Scaffold-time introspection → sidecar manifest (`cli.py`)

The apps + model-serving scaffolds already resolve a `(catalog, schema, table)`
against a live workspace (`_discover_default_data` / `_probe_first_table`,
`WorkspaceClient(profile=…)`). Extend that step: after resolving the schema,
call the existing `introspect_schema(ws, catalog, schema)` →
`{table: ["col(type)", …]}` and write a manifest into the generated project:

```json
{
  "catalog": "samples",
  "schema": "tpch",
  "tables": {
    "customer": ["c_custkey(bigint)", "c_name(string)", "c_acctbal(decimal)", …],
    "orders":   ["o_orderkey(bigint)", "o_custkey(bigint)", "o_totalprice(decimal)", …]
  }
}
```

Path: `.apx/schema.json` at the project root. Best-effort: if introspection
returns `{}` (no warehouse, perms, network), **no manifest is written** and the
agent keeps today's behavior — no regression, no scaffold failure.

The manifest stores **only structured schema** — not the rendered instruction
string — so the manifest is the single source of truth and any future
instruction/card formatting change applies without re-scaffolding.

### 2. DataAgent auto-loads the manifest (`data_agent.py`, `_schema.py`)

- New optional `schema: dict | None` parameter on `DataAgent` (and the shared
  `_build_data_tools_and_instructions`). Shape: `{table: [columns]}` (the same
  shape `introspect_schema` returns).
- A loader `load_baked_schema(start: str | Path | None = None) ->
  dict | None` (in `_schema.py`) walks up from the given start dir (default:
  current working directory) to find `.apx/schema.json`, returns its `tables`
  dict (and exposes `catalog`/`schema` if needed), or `None`.
- Resolution order in `_build_data_tools_and_instructions`:
  1. explicit `schema=` arg, else
  2. live `introspect_schema(ws, …)` when `ws` is given, else
  3. `load_baked_schema()` auto-discovery, else
  4. `{}` (today's generic fallback).
- When a non-empty schema is resolved (from any source), the tables are declared
  as governed `uc_table` resources on the SQL tool (existing `attach_resources`
  path) and ground the instructions — **without needing `ws` at boot**.

The served app runs from the project root, so `.apx/schema.json` is found via
auto-discovery; `agent.py` is unchanged:
`DataAgent("samples", "tpch", extra_tools=[sample_customer], name="…")`.

### 3. Grounded instructions that don't re-discover (`_schema.py::build_instructions_from_schema`)

When `tables` is non-empty:

- **List the tables with their columns** (cap per-table columns to keep the
  prompt bounded — e.g. first ~12 columns, "+N more" — and cap total tables
  similarly with a "+N more tables" note for large schemas).
- **Drop** the *"At the start of every session, call the SQL tool to confirm
  what tables and columns are available before answering questions"* line.
- Replace it with a pre-grounded directive, e.g. *"You already know the schema
  below — query the relevant table directly. Do not run SHOW TABLES / DESCRIBE
  to discover structure."*

The no-tables branch is unchanged (still tells the agent to discover, since it
genuinely has nothing).

### 4. Landing data card (`_ui_chat.py::_render_landing` + read path)

The chat landing (the `#126` landing) renders a card:

> **I understand `samples.tpch`** — 8 tables
> • customer — c_custkey, c_name, c_acctbal, c_mktsegment …
> • orders — o_orderkey, o_custkey, o_totalprice …
> • lineitem — … *(+5 more)*

Source: the same manifest. `_render_landing` already receives the
`AgentContext` (it builds capability cards from `ctx.tools`). The manifest is
loaded once at serve time (via `load_baked_schema`) and attached to the
`AgentContext`, so the landing renders the card from the context with no extra
route or fetch. The card is omitted cleanly when no manifest exists.

### 5. Refresh command (`cli.py`)

`apx refresh-schema` (run inside a scaffolded project) re-introspects the
project's `catalog.schema` against the current workspace and rewrites
`.apx/schema.json`. Staleness of a baked example is acceptable; this is the
escape hatch when the schema drifts.

## Packaging / deploy

- `.apx/schema.json` **must be committed** — the scaffolded `.gitignore` must not
  exclude `.apx/`.
- The Databricks Apps deploy bundle (`.build/` staging, see the manifest-staging
  work) **must include `.apx/schema.json`** so the deployed app is grounded too.

## Data flow

```
apx scaffold (ws)  ──introspect_schema──▶  .apx/schema.json  (git-committed)
                                                  │
                              ┌───────────────────┴───────────────────┐
                  served DataAgent (no ws)                     chat landing
            auto-discover → grounded instructions          read manifest → data card
            + uc_table resources (no warehouse at boot)
```

## Error handling / degradation

- Introspection failure at scaffold → no manifest → DataAgent uses the generic
  fallback (current behavior). No crash, no scaffold failure.
- Missing/corrupt manifest at runtime → loader returns `None` → generic fallback;
  landing omits the card.
- Stale manifest → `apx refresh-schema`. (Out of scope: automatic drift
  detection.)

## Out of scope

- Runtime/startup live introspection or hybrid refresh (explicitly rejected in
  brainstorm — boot cost + warehouse-at-boot on FEVM).
- Carrying the schema in `[tool.apx.agent]` config (rejected: TOML bloat).
- Automatic staleness/drift detection.
- Changing the non-data agent templates.

## Testing

- **Scaffold:** with a stubbed workspace returning a schema, scaffold writes
  `.apx/schema.json` with the expected `{catalog, schema, tables}`; with
  introspection returning `{}`, no manifest is written and scaffold still
  succeeds.
- **`build_instructions_from_schema` (grounded):** given tables+columns, the
  output contains the table names and (capped) column names AND does **not**
  contain the "confirm what tables and columns are available" discovery line;
  contains the "query directly / do not SHOW TABLES" directive.
- **`load_baked_schema`:** finds `.apx/schema.json` by walking up from a nested
  dir; returns `None` when absent; tolerant of corrupt JSON.
- **DataAgent auto-load:** constructed with no `ws`/`schema` in a dir containing
  `.apx/schema.json`, its instructions are grounded (table/column names present,
  discovery line absent) and the tables are attached as `uc_table` resources.
- **DataAgent fallback:** no manifest, no `ws` → generic instructions (current
  behavior) — regression guard.
- **Landing card:** renders the tables/columns from a manifest; omitted when no
  manifest.

## Affected files

- `python/src/apx_agent/cli.py` — scaffold manifest write; `apx refresh-schema`;
  ensure `.apx/` committed + staged for deploy; `.gitignore` template.
- `python/src/apx_agent/_schema.py` — `load_baked_schema`; grounded-instruction
  rewrite (list columns, drop discovery line).
- `python/src/apx_agent/data_agent.py` — `schema=` param + resolution order.
- `python/src/apx_agent/_ui_chat.py` — landing data card from `AgentContext`.
- context wiring (where `AgentContext` is built for serving) — load the manifest
  once and attach it to the context.
- Tests across `tests/test_cli.py`, `tests/test_schema.py` (new if absent),
  `tests/test_data_agent.py`, `tests/test_dev_ui_routes.py`.
