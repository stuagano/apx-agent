# Fleet Operations for apx-agent — Design

**Date:** 2026-06-15
**Status:** Approved design, pre-implementation
**Branch:** `feat/fleet-operations`

## Problem

apx-agent has a rich single-agent surface (`deploy`, `run`, `stop`, `hot-swap`,
`canary`, `delete`) but its *workspace-scoped, multi-agent* story is read-only:
`agents list` and `uc topology` inventory the fleet, and `agents delete` mutates
exactly one agent. There is no way to act on **many** agents at once.

Two concrete wants drive this:

1. **Apply tags to existing agents** — label a fleet (`team`, `env`, `owner`,
   cost-center) so it can be organized and selected.
2. **Update agents already in a workspace** — push a change (start with
   re-promoting to the latest registered model version) across a selected set.

The unifying frame is **fleet operations**: *select a set of agents → act on
them in bulk*, with one shared selection model and a uniform safety posture.

## Key facts established during design

- **Databricks Apps have no custom-tags field.** The SDK `App` object exposes
  `description`, `resources`, `compute_size`, etc. — no arbitrary key/value
  tags. The entire `apx.*` tag taxonomy lives on **UC registered models /
  model versions** (`set_model_version_tag`; read back by `agents list`). An
  App links to its model via the `apx.apps.app_name` version tag. Therefore
  "tag an agent app" **means tag the agent's backing UC registered model** —
  there is no second object to tag.
- **`fleet redeploy` can only work from workspace state.** The workspace
  exposes the deployed artifact + tags, not the source project `deploy` builds
  from. So v1 redeploy re-promotes each agent to its **latest registered UC
  model version**; git-rebuild-from-`apx.apps.git_sha` and config-only patching
  are explicitly deferred to v2.
- **Backfill is inherently partial.** Identity/discovery tags
  (`apx.agent.name`, `apx.apps.app_name`, `apx.serving`) can be reconstructed
  from observable workspace state; rich metadata (`apx.agent.tools`,
  `apx.agent.resources`, `apx.agent.metadata`) cannot be reconstructed for an
  agent that can't be introspected. Backfill stamps the former and says so.

## Command surface

A new top-level group **`apx-agent fleet`** (sibling to `agents`, `uc`,
`traces`, `canary`). v1 subcommands:

```
apx-agent fleet list       # resolve a selection, print a rich inventory (read-only)
apx-agent fleet tag        # set / add / remove user labels across a selection
apx-agent fleet backfill   # stamp missing system discovery tags
apx-agent fleet redeploy   # re-promote each agent to its latest registered version
```

`fleet` lives at the top level (not under `agents`) because these are
workspace-scoped, multi-agent operations — a different mode from single-agent
`agents deploy/run/stop`.

## The shared selector (the spine)

A single resolver module — `_fleet.py` — consumed by all four subcommands. It
reads UC registered models tagged `apx.agent.name` (the same discovery path
`agents list` uses today) and filters by composable predicates:

| Flag | Meaning |
|---|---|
| `--catalog X --schema Y` | UC scope (mirrors `agents list`; `--schema` requires `--catalog`) |
| `--name 'payroll-*'` | name glob against `apx.agent.name` |
| `--where k=v` | tag predicate; repeatable; multiple are AND-ed |
| `--uc-name a.b.c` | explicit registered-model selection; repeatable; bypasses filters |

`--where` matches a tag in **either** namespace (`apx.label.k` or `apx.agent.k`)
transparently, so a freshly-applied label is immediately selectable.

The resolver returns a list of resolved agents, each carrying: `uc_name`,
`latest_version`, `app_name` (from `apx.apps.app_name`), `endpoint` (from
`apx.agent.model`), and the full tag dict.

**`agents list` is refactored to call this same resolver** so there is one
discovery path and no duplicate inventory command. `agents list`'s existing
output/columns are preserved (behavior-preserving refactor).

## Tag model — two namespaces

- **`apx.agent.*` / `apx.apps.*`** — *system tags*, written by `deploy` and
  `backfill`. **Reserved**: `fleet tag` refuses to `--set` or `--remove` any
  key in these namespaces, protecting the selector's own `apx.agent.name`
  invariant (a bulk `--remove` must never be able to strip the tag the resolver
  depends on).
- **`apx.label.*`** — *user labels*. `fleet tag --set team=revops` writes
  `apx.label.team`; `--remove team` strips `apx.label.team`. Bare keys entered
  by the user are mapped into the `apx.label.` prefix automatically.

User labels are written at the **registered-model level** (not the model-version
level) — the same level the selector reads (`agents list` iterates
`registered_models.list()` and reads `m.tags`). Writing labels at version level
would make them invisible to the resolver. This matches where
`_watchdog.set_uc_tags_for_agent` writes the `apx.agent.*` discovery tags;
version-level `apx.apps.*` tags (per `_apps_registry.py`) remain version-scoped
and are out of scope for `fleet tag`.

## Per-command behavior

### `fleet list`
Resolves the selection and prints a table: agent, uc_name, latest version, app,
endpoint, user labels. Read-only. `--format text|json`.

### `fleet tag`
- `--set k=v` (repeatable) → writes `apx.label.k=v`.
- `--remove k` (repeatable) → removes `apx.label.k`.
- Refuses any key resolving into `apx.agent.*` / `apx.apps.*` with a clear error.
- Mutating → dry-run by default (see Safety).

### `fleet backfill`
Stamps *observable* system tags onto agents missing them: `apx.agent.name`,
`apx.apps.app_name`, `apx.serving`. Explicitly partial — it does not and cannot
reconstruct `apx.agent.tools` / `resources` / `metadata`; output states which
tags were stamped and notes the metadata it could not reconstruct. Mutating →
dry-run by default.

### `fleet redeploy`
For each selected agent: find its latest registered UC model version and
re-promote the serving endpoint / app to it. Reports `old → new` version per
agent. v1 source of truth is workspace state only (no git rebuild, no config
patch). Mutating → dry-run by default; additionally honors `--fail-fast`.

## Safety (uniform across all mutating commands)

- **Dry-run by default.** Every mutating command resolves and prints the exact
  per-agent plan and changes nothing unless `--apply` is passed.
- **Continue + report.** A single agent's failure does not strand the rest;
  per-agent results are collected and printed as a
  `succeeded / failed / skipped` summary. **Exit non-zero if any agent failed.**
- `fleet redeploy` additionally honors `--fail-fast` (stop at first error,
  leave the remainder untouched).
- All mutating commands accept `--profile` (Databricks CLI profile), consistent
  with the rest of the CLI.

## Testing

**Unit (no live workspace):**
- Selector predicate logic against a fake registered-models list: scope filter,
  name glob, `--where` AND semantics, `--where` matching both namespaces,
  explicit `--uc-name` bypass.
- Reserved-namespace refusal: `fleet tag --set apx.agent.name=x` and
  `--remove apx.agent.name` both error without writing.

**Reality (ctk-style):**
- Tag round-trip: `fleet tag --set team=revops --apply` then resolve
  `--where team=revops` finds the agent; `fleet tag --remove team --apply`
  then it is gone.
- Dry-run writes nothing: run each mutating command without `--apply` and
  assert no tag/serving mutation occurred.
- Summary exit code: when a simulated agent fails, the command exits non-zero
  and the summary reports it under `failed`.

## Out of scope (v2+)

- `fleet promote` / `fleet rollback` (canary lifecycle across a set).
- `fleet stop` / `fleet delete` (bulk teardown).
- `fleet redeploy` via git-rebuild from `apx.apps.git_sha`.
- `fleet redeploy` config-only patching (scale, env vars, alias).
- Tagging anything other than the backing UC registered model.
