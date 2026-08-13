# PRD: Pre-Call Brief Agent (reusable, Mirion first)

**Version**: 1.1 | **Status**: Draft | **Date**: 2026-08-13

## Summary

A reusable, config-driven apx-agent that lets a Mirion field rep type a company
name into a Databricks App chat surface and get a 1–2 page pre-call brief — open
orders, RMAs, PPRs, opportunities, field notes, overdue actions — synthesized
from four source systems, rendered as markdown on screen and as a downloadable
PDF. The agent reads **governed Unity Catalog views only**. On the sandbox
`fevm-hvhhmh` **all four sources (SFDC, ServiceMax, SharePoint, SAP) are
synthetic tables** to a frozen schema — none are in UC — so the build is fully
self-contained. The agent is driven by a per-customer TOML file, so a second
customer is a new TOML + schema, no code fork. Primary success metric: a correct
7-section brief for a seeded company, produced headless, with a valid PDF, and
deployable as a Databricks App. Source of truth: `docs/design/pre-call-brief-agent.md`.

## Background

Mirion field reps (20–25) manually assemble pre-call context across Salesforce,
ServiceMax, SharePoint, and SAP (~30 min/visit). The V4C proposal
(`~/Documents/Customer/Mirion/mirion_field_brief_technical_v4c.md`) specced a
bespoke Databricks App. This PRD re-architects that proposal onto **apx-agent**
("declared, not wired"), where most of it is assembly on existing primitives:

| App proposal | apx-agent primitive | New code |
|---|---|---|
| Databricks App + UI | `agents deploy --target apps` (ResponsesAgent + chat UI, `_responses_agent.py`) | none |
| Read UC views | `DataAgent` + `sql_tool()`, schema-grounded (`data_agent.py`, `sql_tools.py`, `_schema.py`) | config only |
| Per-rep security | OBO built-in (`_obo.py`) → per-request `WorkspaceClient` → UC RLS | none |
| LLM summarization | FMAPI default (`_llm.py`); inference stays in Databricks | none |
| Audit logging | MLflow spans (`_mlflow_tracing.py`, `_audit.py`) | none |
| **PDF generation** | **none — real gap** | **`render_pdf` tool (WeasyPrint, ReportLab fallback)** |
| Email delivery | `http_tool()` over UC connection | deferred |

`render_pdf` is the only real net-new capability — no `render_pdf`, `weasyprint`,
`precall`, or `mirion` symbols exist in the tree today.

## Research Inputs

- **Phase A codebase research** (this repo) confirmed every extension point in
  the design doc and the exact modules/lines below.
- Skills to invoke during implementation (not yet run): `databricks-synthetic-data-gen`
  (Faker-based synthetic tables for **all four** sources), `databricks-unity-catalog`
  (view DDL, dynamic views / column masks), `databricks-apps` (`--target apps`
  deploy). Databricks work routes through `databricks-core` first, then the product skill.

## Goals

- G1: A `PreCallBriefAgent` that, given a company name, returns a markdown brief
  containing **all 7 sections** with correct per-company data, built from a
  per-customer TOML + baked `schema.json`, no per-source sub-agent fan-out.
- G2: A `render_pdf` tool factory (WeasyPrint, ReportLab fallback) that turns a
  markdown brief into a valid PDF, registered like every other tool factory.
- G3: Reusability — a second customer is a new `customers/<name>.toml` + their own
  `<name>_precall` UC schema, with **zero agent code change**.
- G4: The 7-view UC contract frozen so synthetic→real is a view repoint, not an
  agent change.
- G5: Deployable via `agents deploy --target apps` with OBO scoping data per rep.

## Non-Goals (explicit scope boundaries)

- Real ingestion pipelines for **any** source (SFDC, ServiceMax, SharePoint, SAP)
  — synthetic tables stand in behind the frozen view contract until real ingestion
  lands in a real workspace (a separate, future workstream).
- Email delivery (Phase 6, deferred).
- Per-source sub-agent fan-out / `ParallelAgent` — premature for 7 small reads.
- Calendar auto-trigger, Teams bot, People.ai integration, dedup logic.
- Changing any existing apx-agent primitive — this is assembly + one new tool.
- Building against a real customer workspace unattended (sandbox `fevm-hvhhmh` only).

## Requirements

### Functional

- FR-1: Freeze the UC view contract as 7 view DDLs under `main.mirion_precall`,
  every view keyed by `company`, with the exact columns in the contract table below.
- FR-2: Generate synthetic tables for **all four** sources (SFDC, ServiceMax,
  SharePoint, SAP) to that contract, all keyed by **one shared company set** so
  every section joins cleanly (`databricks-synthetic-data-gen`). The 7 views wrap
  the synthetic tables.
- FR-3: Define the 7 views over the synthetic tables, conforming exactly to the
  contract columns; the agent binds to **view names only** so a later
  synthetic→real repoint of any view is invisible to the agent.
- FR-4: `PreCallBriefAgent` = a single `DataAgent` grounded on all 7 views, with
  per-section instructions and a baked `.apx/schema.json`, driven by
  `customers/mirion.toml`; headless, returns a markdown brief.
- FR-5: `render_pdf` tool factory (new module `python/src/apx_agent/render_pdf.py`)
  following the `build_tool` convention; registered in `_tool_config.py._registry()`
  under type `"render_pdf"` so it is usable from `[[tool.apx.tools]]`.
- FR-6: Deploy `--target apps` with the built-in chat UI; OBO passes rep identity
  per hop so UC RLS/masks scope data per rep.

### Non-functional

- NFR-1: Brief returns in < 30s for a seeded company (design target; verified
  manually against the deployed app, not in the offline gate).
- NFR-2: Compliance (Mirion = DOE / export-control sensitive): masking pushed to
  UC dynamic views / column masks on the 7 views (enforced for any caller);
  inference stays in Databricks (FMAPI); audit via MLflow spans. No agent-code masking.
- NFR-3: `render_pdf` must be serverless-safe on Databricks App compute. WeasyPrint
  is preferred; if its native libs are unavailable on serverless, fall back to
  ReportLab. The offline gate asserts the produced bytes are a valid PDF regardless
  of engine.

## Design

### Architecture

```
Rep (browser, Databricks SSO)
   │ "brief me on Acme Corp"
   ▼
Databricks App  (apx-agent --target apps)   ← ResponsesAgent + apx chat UI
   │ OBO: X-Forwarded-Access-Token (rep identity flows down)
   ▼
PreCallBriefAgent (DataAgent)
   • grounded on customer's UC schema (baked .apx/schema.json)
   • brief-section spec in instructions
   • reads customers/<name>.toml
   ├─ sql_tool         → reads UC views
   ├─ render_pdf       → WeasyPrint / ReportLab fallback (NEW)
   └─ email tool       → deferred
   ▼
Unity Catalog: main.<customer>_precall.vw_*
   ALL 7 views over SYNTHETIC tables on fevm-hvhhmh (swap→real later, no agent change)
   RLS / column masks enforced at the view layer, not in agent code
```

Key files/modules to build on (verified in Phase A):

- **Agent**: `python/src/apx_agent/data_agent.py:300` — `DataAgent(catalog, schema,
  *, warehouse_id=, ws=, instructions=, extra_tools=, ...)`, an `LlmAgent` subclass.
  `DataTemplate` (`data_agent.py:497`) is the config-driven builder (`Spec`:
  `catalog`, `schema` (alias for `schema_name`), `warehouse_id`).
- **Schema grounding**: `python/src/apx_agent/_schema.py` — `introspect_schema`,
  `build_instructions_from_schema`, `load_baked_schema` (reads `.apx/schema.json`).
- **SQL tool**: `python/src/apx_agent/sql_tools.py:23` — `sql_tool(warehouse_id=…)`,
  runs as the calling user (OBO), declares `warehouse_id` as a `ResourceSpec`.
- **Tool-factory convention**: `python/src/apx_agent/_tool_factory.py:41` —
  `build_tool(call, *, name, description, resources)`. One module per factory
  (`genie.py`, `catalog.py`, `sql_tools.py`, `vector_search.py`). Idiom: an inner
  `async def _fn(arg, ws: UserClientDependency)` (or `Dependencies.Workspace`),
  **no** `from __future__ import annotations`, returned via `build_tool(...)`.
- **Tool registry / config**: `python/src/apx_agent/_tool_config.py` — `_registry()`
  maps a `type` string to a factory; `[[tool.apx.tools]]` tables in `pyproject.toml`
  are loaded by `merge_config_tools`. Add `"render_pdf": render_pdf` here.
- **DI**: `python/src/apx_agent/_defaults.py:288` — `Dependencies.Workspace`,
  `Dependencies.Sql`, `UserClientDependency`. `@tool` decorator in `_tool.py`.
- **OBO**: `python/src/apx_agent/_obo.py` — per-request `WorkspaceClient` from the
  forwarded access token.
- **Deploy**: `python/src/apx_agent/cli.py` — `apx-agent agents deploy --target apps`
  (built-in chat UI, `_responses_agent.py`); scaffold default `target="apps"`.

### Interface changes

- **New module** `python/src/apx_agent/render_pdf.py` exposing
  `render_pdf(*, name="render_pdf", description=None) -> Callable` — a tool factory
  whose inner callable takes the brief markdown (and optional filename), renders
  HTML→PDF via WeasyPrint (ReportLab fallback), writes/returns the PDF, and is
  stamped via `build_tool`. No workspace resource needed (pure compute) — attach no
  `ResourceSpec`.
- **New registry entry** in `_tool_config.py._registry()`: `"render_pdf"`.
- **New config file** `customers/mirion.toml` (schema below). No new CLI flags.
- **New export** of `render_pdf` from `apx_agent/__init__.py` (mirrors `sql_tool`,
  `genie_tool`, `build_tool` public exports).

### Data model

**Frozen 7-view UC contract** — schema `main.mirion_precall`; every view keyed by
`company` so all sections join on one entity. All 7 views are **synthetic-backed**
on the sandbox; the `Models` column is the real source each view will point at once
ingestion exists. The agent binds to view **names only**; swapping synthetic→real
later = repoint the view definition, zero agent change.

| View | Models | Columns |
|---|---|---|
| `vw_opportunities` | SFDC | `company, opportunity, stage, value, close_date` |
| `vw_actions` | SFDC | `company, action, due_date, status` |
| `vw_winloss` | SFDC | `company, outcome, product, date` |
| `vw_field_notes` | SFDC | `company, note, author, date` |
| `vw_rmas` | ServiceMax | `company, rma_id, description, status, date` |
| `vw_pprs` | SharePoint | `company, ppr_id, description, severity, status` |
| `vw_orders` | SAP | `company, order_id, description, qty, expected_ship, status` |

**Per-customer TOML config schema** (`customers/mirion.toml`):

```toml
# customers/mirion.toml
[precall]
entity       = "company"
catalog      = "main"
schema       = "mirion_precall"
warehouse_id = "…"

[[precall.section]]
title = "Open Orders & Shipping"
view  = "vw_orders"
[[precall.section]]
title = "Open Opportunities"
view  = "vw_opportunities"
# … win/loss, RMAs, PPRs, field notes, overdue actions
```

A second customer = new TOML + their own `<name>_precall` schema. No code fork.

## Acceptance Criteria

ACs map 1:1 to the design-doc build phases 1–6. Gates live under
`python/tests/gates/` with `precall` in each filename so `uv run pytest -k precall`
selects them. Phases 1–4 are offline pytest gates (against synthetic/in-memory
data or parsed DDL/config artifacts — no live workspace). Phase 5 (deployed App +
OBO-per-rep) is **manual**: it needs a live deploy to `fevm-hvhhmh` and a second
signed-in rep identity, which no offline gate can assert. Phase 6 (email delivery +
swap synthetic→real) is a **deferred future workstream** and is not built this cycle.

- [ ] AC-1 (Phase 1 — freeze contract): Given the 7 view DDL/contract artifacts,
  when the gate parses them, then all 7 view names are present and each view's
  column set exactly equals the frozen contract (names + order per the table above).
- [ ] AC-2 (Phase 2 — synthetic data, all four sources): Given the synthetic-data
  generator for all four sources, when it produces rows offline, then every one of
  the 7 views conforms to its contract columns, is non-empty, and every `company`
  value is drawn from **one shared company set** so all 7 sections join cleanly.
- [ ] AC-3 (Phase 3 — agent): Given `PreCallBriefAgent` built from
  `customers/mirion.toml` + baked `.apx/schema.json` and a seeded company backed by
  stubbed SQL, when it runs headless, then the returned markdown brief contains all
  7 section titles and the seeded per-company data values for that company.
- [ ] AC-4 (Phase 4 — PDF): Given a markdown brief, when `render_pdf` is invoked,
  then it produces a PDF that `ctk.verify(Artifact(...))` confirms is non-empty and
  starts with the bytes `%PDF`, and `"render_pdf"` resolves from
  `_tool_config._registry()`.
- [ ] AC-5 (Phase 5 — deploy + OBO, MANUAL): Given the agent deployed via
  `agents deploy --target apps` to `fevm-hvhhmh`, when two different reps use the
  chat UI, then each sees only data their UC grants/RLS permit (OBO scoping),
  verified manually.
- [ ] AC-6 (Phase 6 — email + swap synthetic→real, DEFERRED): Given the frozen
  contract, when a source's real ingestion later lands in a real workspace, then
  repointing that view (and adding email delivery) requires **zero agent code
  change** — deferred future workstream, not built or gated this cycle.

Bad: 'brief works'. Good (AC-4): 'render_pdf output starts with the bytes `%PDF`
and is > 1KB'. Good (AC-1): 'vw_orders columns == [company, order_id, description,
qty, expected_ship, status]'.

## Risks

- R1: **WeasyPrint unavailable / unrenderable on serverless App compute** →
  Phase 4 blocked. Mitigation: verify WeasyPrint import+render on serverless early;
  fall back to ReportLab if native libs are missing; the AC-4 gate is engine-agnostic
  (asserts valid PDF bytes). Escalate if neither engine renders on serverless.
- R2: **Synthetic company keys inconsistent across the four sources** → joins
  return empty briefs. Mitigation: AC-2 asserts every `company` in all 7 views is
  drawn from one shared company set.
- R3: **Contract drift** between the 7 views. Mitigation: AC-1 freezes the column
  contract; all 7 views are checked against it.
- R4: **apx-agent `DataAgent`/`sql_tool` API differs from the design's assumptions**
  → Phase 3 stalls. Mitigation: Phase A pinned exact signatures/paths; escalate if
  the live API diverges (see `escalate_on`).

## Open Questions

- [ ] Where do the frozen view DDLs live as verifiable artifacts — a `sql/` dir of
  `.sql` files, or a single contract manifest (e.g. `customers/mirion_contract.py`/
  `.toml`) the gate imports? (Affects AC-1 gate shape.)
- [ ] Source of the shared company set for AC-2 keying — a checked-in seed list
  (no real system dependency, all synthetic) is the default; confirm the seed
  company names to use for Mirion.

---

## Agent Handoff

> Machine-readable block for relentless and other autonomous agents.

```json
{
  "prd_version": "1.1",
  "goal": "Ship a config-driven PreCallBriefAgent that returns a correct 7-section markdown brief for a seeded company, renders it to a valid PDF via a new render_pdf tool, and is deployable --target apps — reusable per customer via TOML + UC schema with zero agent code fork; all four sources synthetic on fevm-hvhhmh.",
  "success_criteria": [
    "AC-1: 7-view UC contract frozen with exact columns",
    "AC-2: synthetic tables for all four sources conform to contract, keyed by one shared company set so all sections join",
    "AC-3: PreCallBriefAgent returns markdown brief with all 7 sections + seeded data",
    "AC-4: render_pdf produces a valid PDF and is registered in the tool registry",
    "AC-5: deployed --target apps, OBO scopes data per rep (manual)",
    "AC-6: email delivery + swap synthetic->real behind the contract (deferred future workstream)"
  ],
  "convergence": {
    "stopping_signal": "cd python && uv run pytest -k precall",
    "progress_metric": "failing gate count",
    "known_ceiling": "none data-side (all four sources are synthetic on fevm-hvhhmh, so the build is self-contained); WeasyPrint may be unavailable on serverless App compute -> ReportLab fallback.",
    "re_represented": false
  },
  "acceptance_criteria": [
    { "id": "AC-1", "description": "7 view DDL/contract artifacts define all 7 view names with exactly the frozen contract columns", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_precall_contract.py", "gate_test": "test_precall_view_contract_frozen" },
    { "id": "AC-2", "description": "synthetic tables for all four sources conform to the 7-view contract, are non-empty, and every company is drawn from one shared company set so all sections join", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_precall_synthetic.py", "gate_test": "test_precall_synthetic_conforms_and_keyed" },
    { "id": "AC-3", "description": "PreCallBriefAgent built from customers/mirion.toml + baked schema returns markdown brief with all 7 section titles and seeded per-company data (stubbed SQL)", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_precall_agent.py", "gate_test": "test_precall_brief_has_all_seven_sections" },
    { "id": "AC-4", "description": "render_pdf renders a markdown brief to a PDF (ctk.verify: non-empty, starts with %PDF) and resolves from _tool_config._registry() under 'render_pdf'", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_precall_render_pdf.py", "gate_test": "test_precall_render_pdf_produces_valid_pdf" },
    { "id": "AC-5", "description": "agent deployed --target apps to fevm-hvhhmh; OBO scopes data per rep so two reps see only their UC-permitted data", "verifiable": false, "test_type": "manual", "skip_reason": "requires a live deploy and a second signed-in rep identity; no offline gate can assert OBO/RLS scoping" },
    { "id": "AC-6", "description": "email delivery + swap synthetic->real ingestion behind the frozen contract, per source, in a real workspace", "verifiable": false, "test_type": "manual", "skip_reason": "deferred future workstream; not built or gated this cycle (design-doc phase 6, 'later')" }
  ],
  "must_have": [
    "FR-1: freeze 7-view UC contract keyed by company",
    "FR-2: synthetic tables for all four sources, keyed by one shared company set, views wrap them",
    "FR-4: PreCallBriefAgent = single DataAgent + sql_tool + TOML + baked schema, headless markdown brief",
    "FR-5: render_pdf tool factory (python/src/apx_agent/render_pdf.py) registered in _tool_config._registry()"
  ],
  "out_of_scope": [
    "real ingestion pipelines for any source (all four synthetic stand-ins)",
    "email delivery (deferred design-doc phase 6)",
    "per-source sub-agent fan-out / ParallelAgent",
    "calendar auto-trigger, Teams bot, People.ai, dedup logic",
    "modifying existing apx-agent primitives"
  ],
  "constraints": {
    "tech_stack": "Python 3.10+, uv, pytest (asyncio_mode=auto via pytest-asyncio), ctk kit (pythonpath=[.ctk]); apx-agent framework; Databricks UC + SQL warehouse + Apps (serverless); WeasyPrint (new dep, ReportLab fallback) for PDF.",
    "key_files": [
      "python/src/apx_agent/data_agent.py",
      "python/src/apx_agent/_schema.py",
      "python/src/apx_agent/sql_tools.py",
      "python/src/apx_agent/_tool_factory.py",
      "python/src/apx_agent/_tool_config.py",
      "python/src/apx_agent/_defaults.py",
      "python/src/apx_agent/_obo.py",
      "python/src/apx_agent/_responses_agent.py",
      "python/src/apx_agent/cli.py",
      "python/src/apx_agent/__init__.py",
      "python/src/apx_agent/render_pdf.py (NEW)",
      "customers/mirion.toml (NEW)",
      "python/tests/gates/ (NEW dir; filenames must contain 'precall')"
    ],
    "patterns": "One module per tool factory; inner `async def _fn(arg, ws: UserClientDependency)`, NO `from __future__ import annotations` in factory modules, return via build_tool(call, name=, description=, resources=[ResourceSpec(...)]); register type-string in _tool_config._registry(); DataAgent grounds via ws introspection or baked .apx/schema.json; config via [tool.apx.agent]/[[tool.apx.tools]] in pyproject; tests are claim-vs-reality (*_reality_ctk.py / ctk.verify(Artifact(path, min_bytes=, must_contain=))); gate filenames contain 'precall' so `pytest -k precall` selects them; make check is the read-after-write gate."
  },
  "preferred_skills": [
    "databricks-synthetic-data-gen",
    "databricks-unity-catalog",
    "databricks-apps"
  ],
  "escalate_on": [
    "WeasyPrint unavailable on serverless and ReportLab fallback also fails to render (blocks Phase 4 PDF)",
    "apx-agent DataAgent/sql_tool API differs from design assumptions (signatures/paths pinned in Phase A no longer hold)",
    "no shared company seed set available to key synthetic data (blocks AC-2)",
    "ambiguity in where frozen view DDLs live as verifiable artifacts (Open Question 1)",
    "any request to build unattended against a real customer workspace instead of fevm-hvhhmh",
    "conflicting or under-specified acceptance criteria",
    "architectural decision not covered in this PRD or the design doc"
  ],
  "loop_guards": {
    "max_iterations": 8,
    "state_hash_check": true,
    "heartbeat_interval_seconds": 30,
    "on_stuck": "pause_and_surface",
    "on_no_progress": "stop_and_escalate",
    "state_persistence": "local_disk"
  }
}
```

IMPORTANT: This PRD references actual apx-agent file paths and conventions verified
during Phase A research (see key_files / patterns). The design doc
`docs/design/pre-call-brief-agent.md` is the source of truth. All four sources
(SFDC, ServiceMax, SharePoint, SAP) are synthetic on `fevm-hvhhmh`; there is no
real-SFDC phase and no "SFDC unreachable" ceiling.
