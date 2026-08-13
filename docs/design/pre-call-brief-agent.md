# Design: Pre-Call Brief Agent (reusable, Mirion first)

**Status:** Design agreed 2026-08-13. Implementation → `/relentless-prd` → `/relentless-orchestrate`.
**Origin:** V4C-authored "Pre-Sales Call Intelligence App" brief for Mirion
(`~/Documents/Customer/Mirion/mirion_field_brief_technical_v4c.md`). This re-architects that
proposal onto apx-agent instead of a bespoke Databricks App.

## Goal

A rep types a company name and gets a 1–2 page pre-call brief (open orders, RMAs, PPRs,
opportunities, field notes, overdue actions) synthesized from four source systems, rendered
on screen and as a downloadable PDF. Config-driven so it generalizes across customers; Mirion
is instance #1.

## Decisions (locked)

1. **Interface:** apx-agent + minimal UI — deploy `--target apps`, use the built-in chat
   surface. No separate app layer.
2. **Data access:** UC-first. The agent reads governed Unity Catalog views ONLY. On the
   sandbox `fevm-hvhhmh` none of the four sources are in UC, so **all four (SFDC, ServiceMax,
   SharePoint, SAP) are synthetic tables** to a frozen schema. When real ingestion lands in a
   real workspace, the views repoint at real tables — zero agent change.
3. **Scope:** reusable, config-driven per customer; Mirion first.

## Why this is mostly assembly

apx-agent is a "declared, not wired" framework. Most of the app's proposal already exists:

| App proposal | apx-agent primitive | New code |
|---|---|---|
| Databricks App + UI | `agents deploy --target apps` (ResponsesAgent + chat UI) | none |
| Read UC views | `DataAgent` + `sql_tool()`, schema-grounded | config only |
| Per-rep security | OBO built-in (`_obo.py`) → per-request WorkspaceClient → UC RLS | none |
| LLM summarization | FMAPI default (`_llm.py`); inference stays in Databricks | none |
| Audit logging | MLflow spans (`_mlflow_tracing.py`, `_audit.py`) | none |
| **PDF generation** | **none — real gap** | **`render_pdf` tool (WeasyPrint)** |
| Email delivery | `http_tool()` over UC connection (ACS) | thin, deferable |

## Architecture

```
Rep (browser, Databricks SSO)
   │ "brief me on Acme Corp"
   ▼
Databricks App  (apx-agent --target apps)   ← ResponsesAgent + apx chat UI
   │ OBO: X-Forwarded-Access-Token (rep identity flows down)
   ▼
PreCallBriefAgent (DataAgent)
   • grounded on customer's UC schema (baked schema.json)
   • brief-section spec in instructions
   • reads customers/<name>.toml
   ├─ sql_tool         → reads UC views
   ├─ render_pdf       → WeasyPrint (NEW)
   └─ email tool       → deferred
   ▼
Unity Catalog: main.<customer>_precall.vw_*
   ALL views over SYNTHETIC tables on fevm-hvhhmh (swap→real later, no agent change)
   RLS / column masks enforced at the view layer, not in agent code
```

**Agent shape:** a single `DataAgent` grounded on all views, brief sections in its
instructions; the LLM writes per-section SQL. No per-source sub-agent fan-out
(`ParallelAgent` exists but is premature for 7 small reads) — add only if latency/quality
demands it.

## UC view contract (freeze first — this is the real deliverable)

Schema `main.mirion_precall`; every view keyed by `company` so all sections join on one entity.

All views are synthetic-backed on the sandbox; the `Models` column is the real source each
view will point at once ingestion exists.

| View | Models | Columns |
|---|---|---|
| `vw_opportunities` | SFDC | company, opportunity, stage, value, close_date |
| `vw_actions` | SFDC | company, action, due_date, status |
| `vw_winloss` | SFDC | company, outcome, product, date |
| `vw_field_notes` | SFDC | company, note, author, date |
| `vw_rmas` | ServiceMax | company, rma_id, description, status, date |
| `vw_pprs` | SharePoint | company, ppr_id, description, severity, status |
| `vw_orders` | SAP | company, order_id, description, qty, expected_ship, status |

Agent binds to view names only. Swap synthetic→real later = repoint the view definition,
**zero agent change.**

## Config schema (reusability)

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
# … RMAs, PPRs, field notes, overdue actions
```

Second customer = new TOML + their own `<name>_precall` schema. No code fork.

## Compliance (Mirion = DOE / export-control sensitive)

Stronger than the app's plan by pushing controls to UC:
- **Pre-LLM masking** → UC dynamic views / column masks on the 7 views (enforced for any
  caller, not just this agent).
- **Inference stays in Databricks** → FMAPI default; nothing egresses.
- **Audit** → MLflow spans (built-in).

## Build phases (→ relentless acceptance criteria)

1. Freeze the UC view contract (7 view DDLs).
2. Synthetic tables for **all four** sources to that schema, all keyed by a shared set of
   company names so every section joins cleanly (`databricks-synthetic-data-gen`). Views wrap
   the synthetic tables.
3. `PreCallBriefAgent`: DataAgent + sql_tool + config + section instructions + baked
   schema.json; headless, returns markdown brief. Verifiable: brief for a seeded company
   contains all 7 sections with correct data.
4. `render_pdf` tool (WeasyPrint, serverless-safe).
5. Deploy `--target apps` + chat UI; verify OBO scopes data per rep (manual).
6. *(later)* email delivery; swap synthetic→real ingestion behind the contract, per source, in
   a real workspace.

## Target workspace

Build target: **sandbox `fevm-hvhhmh`** (never build unattended against a real customer
workspace). All four synthetic source tables + the 7 views + the deployed App live there —
fully self-contained, no dependency on any real source system being in UC. Real ingestion is a
future, separate workstream in a real workspace; it repoints the views and needs no agent change.
