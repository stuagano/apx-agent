# GyanSys Staffing Coworker — Phase-1 Demo & Architecture

**Date:** 2026-06-22
**Status:** Design approved, pending spec review
**Target workspace:** `fe-stable` (profile)
**Built with:** apx-agent `CoworkerAgent`

## Context

GyanSys wants an internal, **read-only** Databricks application that combines
Salesforce opportunity data with Replicon resource (people/skills/availability)
data so practice leadership can match the right people to the right work, see
stalled pipeline, and understand staffing bandwidth — without buying more
Salesforce licenses. Salesforce stays the system of record; phase 1 never writes
back.

This deliverable is two things:
1. A **runnable demo agent** (the centerpiece of the technical deep dive).
2. An **architecture doc** covering the phase-1 build and the broader platform
   talking points.

Users are business/delivery leaders (mid-management, practice leads, directors,
VPs) across the U.S., India, the Philippines, and South America — not engineers.

## Goals / Non-goals

**Goals**
- A working `CoworkerAgent` that answers, against live synthetic data on
  `fe-stable`: "who's the best-fit available person for this opportunity?",
  "which opps are stalled and why?", "where's our bandwidth?"
- Vector-similarity (nearest-neighbor) matching of opportunities → people via
  Databricks Vector Search.
- Schema-grounded SQL tools (no hallucinated columns) — the anti-hallucination
  story.
- An architecture doc mapping each customer ask to the build, plus doc-only
  talking points (predictive, ingestion, cost, platform boundaries).

**Non-goals (phase 1 / YAGNI for the demo)**
- No write-back to Salesforce.
- No live SF/Replicon connectors — synthetic seed data only.
- No predictive model built (designed-only talking point).
- No multi-agent supervisor (one coworker covers the story).
- No production hardening / RBAC beyond what Databricks Apps + UC give by default.

## Architecture (phase-1 data flow)

```
Salesforce ──(Lakeflow Connect / synthetic seed)──┐
                                                   ├─► UC Delta tables ──► Vector Search index (people skills)
Replicon  ──(batch sync / synthetic seed)─────────┘          │
                                                              ▼
                                              apx-agent CoworkerAgent (read-only)
                                                  ├─ vector match tool  → nearest-neighbor people
                                                  ├─ grounded SQL tools → stalled opps, bandwidth, pipeline
                                                  └─ (optional) Genie space for free-form NL
                                                              ▼
                                              Databricks App  ──► management users
```

For the demo, ingestion is synthetic seed data. The agent and the App are real.
Live ingestion (Lakeflow Connect for Salesforce, batch sync for Replicon) is an
architecture-doc section, not a build.

**Required workspace stack (`fe-stable`):** Unity Catalog, a serverless SQL
warehouse, a Vector Search endpoint + a managed embedding endpoint
(`databricks-gte-large-en`), the Foundation Model API (agent LLM), and
Databricks Apps (for the deployed version).

## Data model

UC location: `gyansys_demo.staffing` (catalog.schema on `fe-stable`).
~200 people across 4 regions, ~75 opportunities. Synthetic, deterministic,
with deliberately-planted demo moments (a few obviously-stalled high-value opps;
a couple of regions tight on a hot skill, e.g. Databricks/PySpark in India).

### `salesforce_opportunities`
| field | purpose |
|---|---|
| `opportunity_id`, `name`, `account_name` | identity |
| `stage`, `amount`, `probability`, `close_date` | pipeline view |
| `created_date`, `last_activity_date` | stall detection (open stage + no activity > 30 days) |
| `region` | geographic match |
| `required_role`, `required_skills` (text) | match input (embedded for vector search) |
| `stall_reason` (nullable) | concrete answer to "why stalled?" |

### `replicon_people`
| field | purpose |
|---|---|
| `person_id`, `name`, `title`, `practice`, `region` | identity + filter |
| `skills` (text), `certifications` | match target (embedded) |
| `availability_pct` | rank available people; bandwidth-by-region |
| `cost_rate` | profitability talking point |
| `current_project` | realism |

### Vector Search index
Delta-sync index over `replicon_people`, embedding a derived skill-profile
string (`title + skills + certifications`) with `databricks-gte-large-en`.
~200 rows → trivially cheap (supports the cost story).

## The agent

A single `CoworkerAgent` (apx-agent coworker template; a `DataAgent` subclass
that joins two source systems).

- **Persona:** "a resource-planning analyst for practice leadership" — answers
  in management terms (bandwidth, fit, pipeline health), not SQL.
- **Objective:** match opportunities/leads to best-fit available people; surface
  stalled pipeline and staffing bandwidth.
- **Sources:** `salesforce_opportunities` and `replicon_people`.
- **Join model:** the two systems share no ID; they link through the skills
  taxonomy. Matching is nearest-neighbor cosine similarity via Vector Search,
  not an equi-join.

**Tools (all read-only):**
1. `find_people_for_opportunity` — centerpiece. Takes an opportunity (or
   free-text requirement), embeds `required_role + required_skills`, queries the
   Vector Search index over people skill profiles → nearest neighbors, then
   filters/ranks by `availability_pct` and `region`.
2. Grounded SQL tools (baked `.apx/schema.json`) — stalled opps + reasons,
   bandwidth by region/practice, pipeline by stage. The agent knows real columns
   before the first question (anti-hallucination).
3. *(Optional, toggle)* a Genie space tool for open-ended NL beyond the baked
   tools — included if we want to showcase Genie specifically.

**Grounding/memory:** schema baked at scaffold time; memory session-scoped
(read-only analytics agent — nothing to remember across users).

## Demo script (customer ask → agent action)

| Customer ask | Demo moment |
|---|---|
| "Match the right people to the right work" | "Who's the best fit for the Acme data-platform opportunity?" → ranked, available people w/ region + skills |
| "Which opportunities are stalled, and why?" | grounded SQL → open opps, no activity > 30 days + `stall_reason` |
| "Where's our bandwidth?" | "How much availability for Databricks work in India?" → availability % by region/practice |
| "Broaden access without more SF licenses" | the App — mgmt reads pipeline without a Salesforce seat |
| "Grounding / no hallucination" | show baked `.apx/schema.json` — agent cites real columns |

## Run plan

1. **Local dev first** — generate + load synthetic data into
   `gyansys_demo.staffing` on `fe-stable`, create the VS index, scaffold the
   coworker, `apx-agent run` against the dev UI. Fully runnable.
2. **Deploy to a Databricks App** — the deliverable artifact for the deep dive.
   **Known risk:** App deploys to `fe-stable` have hit a bundle-state mismatch
   (bundle pointing at `fe-cowork`, workspace_id/home-dir mismatch). Resolve
   during implementation; fall back to local-only if it blocks the timeline.

## Doc-only sections (talking points, not built)

- **Predictive:** where an MLflow stall-risk / profitability model slots in over
  historical opps.
- **Live ingestion:** Lakeflow Connect (Salesforce), batch sync (Replicon).
- **Cost:** serverless compute + a ~200-row VS index — quotable vs Salesforce
  licenses and traditional hosted apps.
- **Platform boundaries:** external/vendor/attorney access, multi-tenant,
  transactional use cases (React/Python/Postgres alternative) — phase-2+
  evaluation, not phase-1.

## Testing / acceptance

- Synthetic-data generation is deterministic and asserted: expected row counts;
  planted stalled opps exist; at least one region is tight on a hot skill.
- VS index returns the expected nearest-neighbor person for a known
  opportunity's requirement.
- Each agent tool returns grounded rows (real columns, no errors).
- One end-to-end check: a representative management question → a sensible,
  grounded answer.
- Demo is considered "runnable" when all of the above pass against live
  `fe-stable` data via the local dev UI.
