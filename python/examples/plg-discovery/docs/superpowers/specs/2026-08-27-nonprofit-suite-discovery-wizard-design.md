# Agentic Nonprofit Suite — Discovery & Configuration Wizard (Design)

*Design spec — 2026-08-27. Author-driven brainstorming output. Scope: the 2-day demo
prototype ("Slice 1 + teaser"), framed inside the end-to-end north-star vision.*

---

## 1. Vision (north star)

An agent-assembled, **configuration-driven vertical SaaS on the Databricks lakehouse** for
small/medium nonprofits. A nonprofit ops lead has a natural-language **discovery session**
with an agent; the agent turns that conversation into declarative configuration that
parameterizes a **shared library of pre-built components** — rather than writing bespoke
code per tenant.

The system is grounded in the research brief
[`nonprofit-saas-landscape-2025-2026.md`](../../../nonprofit-saas-landscape-2025-2026.md),
which supplies the discovery ontology (§14.1, nine functional domains), the segmenting
questions (§14.2), and the build-vs-buy logic (§14.3/§14.4).

### 1.1 Four layers

1. **Component Catalog (supply).** Each entry = *what it does* + a *declarative config
   schema* + a *bare-bones base implementation* on Databricks primitives + *integration
   seams* to external ("buy") tools. Adapting to a tenant produces a **config document**,
   not code.
2. **Wizards (the experience).**
   - *Discovery → Blueprint*: agent-driven discovery producing a **catalog-aware
     blueprint** that, for each domain, decides against the org's *existing* stack —
     **Keep & Integrate** the current tool vs **Migrate/Retire** it in favor of **Buy**
     (external SaaS) or **Build** (a named catalog component run in Databricks).
   - *Per-component Configuration*: for each Run-in-Databricks component, an agent-guided
     flow that fills that component's config schema. Output: the component's config
     document.
3. **Tenant Configuration Store.** Per tenant: profile → blueprint → component configs.
   At scale this becomes the multi-tenant control plane.
4. **Provisioning.** Instantiate configured components for a tenant (tables, app,
   dashboards, wired integrations).

### 1.2 The key simplification — the catalog reduces to ~2 engines

Following the design principle that components should resemble flexible all-in-one products
(ClickUp / Zoho / Asana), the catalog collapses into **two reusable engines**; each
"component" is a *configuration* of one engine:

- **Engine A — Flexible business-app engine.** Configurable objects, fields, views,
  workflows, bent per tenant via config. *Donor Management, CRM, Volunteer Mgmt, Case Mgmt*
  are instances.
- **Engine B — Ingest → lakehouse → BI engine.** Pull data from an existing system into
  Delta/UC and surface a dashboard. *Finance / Impact Reporting* is an instance.

This makes the two demo teasers representative of the entire catalog, not one-offs.

---

## 2. Scope of this build (the 2-day demo)

**Goal:** a deployed, demoable prototype that tells the end-to-end story: discovery →
catalog-aware blueprint → configure two representative components (one per engine).

### 2.1 In scope

- **Discovery wizard** (agent-driven) → **catalog-aware blueprint** with a keep/build vs
  buy/build decision per domain, made against the org's existing stack (§3.4/§3.5).
- **Component catalog data file**: ~5–8 Run-in-Databricks component specs (name,
  description, config-schema outline); the two teaser components get full config schemas.
- **Teaser 1 — Donor Management (Engine A):** a real, minimal **schema-driven app** on
  Lakebase (1–2 objects: Donor, Gift) whose visible fields/labels + a custom field are set
  by an agent-generated config produced in a short configuration wizard. A live table view
  + add/edit form.
- **Teaser 2 — Finance/Impact Reporting (Engine B):** a **mocked connector** loads a
  **sample QuickBooks export** (CSV/JSON) into a Delta table in UC; a real **Databricks
  AI/BI (Lakeview) dashboard** over that table is surfaced (embed or link).
- **Generic wizard shell** (React): NL conversation panel + **status bar with inspectable
  artifact nodes** + a result surface (blueprint / donor app / dashboard).
- **Background-info intake**: paste text **and** upload a file (parsed to text) **and**
  best-effort auto-fetch of provided links — used to pre-fill the agent's questions and
  avoid the "doctor's-office effect." Link-fetch degrades gracefully on failure.
- **Deployed to Databricks Apps** as a **single app** (FastAPI runs apx-agent in-process
  and serves the React bundle same-origin).

### 2.2 Explicitly out of scope (designed-for, not built)

- Editing/revisiting completed stages (status-bar nodes are **inspectable / read-only**).
- Provisioning beyond the two teasers; a fully generic ClickUp-style app builder; live
  QuickBooks OAuth ingestion.
- Durable multi-tenant persistence, auth beyond the Databricks App default, a real
  user/login/tenant model.
- Activation / PLG event instrumentation (belongs to the multi-tenant future, not the
  demo).

### 2.3 The honesty line

The **narrative spine is fully real**: the agent, both wizards, the generated config,
real Lakebase data, a real Lakeview dashboard on real lakehouse data. What is *deliberately
minimal or mocked*: the app engine renders 1–2 objects (not an infinite builder), and the
QuickBooks **connector** is a sample-file loader (the dashboard result is real).

---

## 3. Architecture

### 3.1 Topology — one Databricks App, agent in-process

```
┌───────────────────────── Databricks App (single deploy) ─────────────────────────┐
│                                                                                   │
│   React wizard (static bundle)                                                    │
│        │  same-origin HTTP                                                        │
│        ▼                                                                          │
│   FastAPI backend                                                                 │
│     • /chat        → relay a turn to the in-process discovery/config agent        │
│     • /ingest      → parse pasted text / uploaded file / fetched link → context   │
│     • /donor/*     → schema-driven CRUD backed by Lakebase                        │
│     • /dashboard   → trigger sample ingest to Delta; return Lakeview embed/link   │
│     • static       → serve the React bundle                                       │
│        │                                                                          │
│        ├── in-process ──► apx-agent (LlmAgent + playbooks + brief grounding)      │
│        ├── Lakebase (Postgres) ──► donor app data + minimal session/artifact state│
│        └── Databricks SDK/SQL ──► Delta table (sample QB data) + Lakeview API     │
└───────────────────────────────────────────────────────────────────────────────────┘
```

Rationale: a deployed Databricks App is a single served unit; running apx-agent in-process
(`run_once` / its Python API) avoids a second deployment and cross-app OAuth. The backend
is **thin plumbing**, not a conversation orchestrator — the agent owns the dialog.

### 3.2 Units and interfaces

| Unit | Responsibility | Depends on |
|---|---|---|
| **Wizard shell (React)** | Generic wizard UI: conversation, status bar w/ artifact nodes, result surface. Parameterized by *wizard type*. | backend HTTP |
| **Backend (FastAPI)** | Serve bundle; relay `/chat`; `/ingest`; donor CRUD; dashboard setup. Stateless-ish. | apx-agent, Lakebase, Databricks SDK |
| **Discovery/Config agent (apx-agent)** | Owns each dialog; follows a **playbook**; emits **typed artifacts**. | model endpoint, brief, catalog |
| **Component catalog (data)** | Machine-readable catalog + config schemas (incl. 2 full teaser schemas). | — |
| **Donor app engine** | Schema-driven CRUD (1–2 objects) rendered from a component config. | Lakebase, backend |
| **Ingest+BI engine** | Sample-file → Delta; Lakeview dashboard. | Databricks SDK, Lakeview API |

Isolation test: the frontend knows only the backend HTTP contract; the backend knows only
the agent's artifact schemas and the two engines' configs; the agent knows only its
playbook + grounding. Any one can change internals without breaking the others.

### 3.3 Agent design (apx-agent)

- A single `LlmAgent` (Databricks Foundation Model endpoint — prefer a strong
  instruction-following model, e.g. Claude on Databricks if available; else Llama).
  Model id pinned at setup.
- **Grounding:** the research brief (as context/knowledge) + the component catalog file.
- **Playbooks (the "modus operandi," superpowers-style):** procedural instructions that
  put the agent in stages, each **gated by a discrete artifact**. Two playbooks:
  1. **Discovery Playbook** — stages: (Intake pre-fill) → **Org Profile** → **Domain
     Relevance** → **Suite Blueprint**.
  2. **Configuration Playbook** — parameterized by a component's config schema; stages fill
     the schema → **Component Config** artifact.
- **Enforcement is prompt-level (soft), by design** for the demo — adequate in a
  controlled demo; hard (code) gates are a production concern, deferred.
- **Structured output:** each stage emits a typed JSON artifact against a fixed schema so
  the frontend can render status-bar nodes and result surfaces deterministically.

### 3.4 Artifact schemas (typed)

- `OrgProfile` — budget tier, staff/volunteer counts, revenue mix, direct-service?, the
  daily vertical workflow, compliance surface (from brief §14.2), **plus a required
  `current_systems` inventory** (see §3.5).
- `DomainRelevance` — the nine domains (§14.1) each scored + rationale.
- `Blueprint` — per needed domain, a **keep/build vs buy/build** decision made against the
  org's *existing* stack (nonprofits already run on tools today):
  `{ domain, current_system, decision, justification }` where
  `decision ∈ { Keep&Integrate, Migrate→Buy, Migrate→Build, New→Buy, New→Build }`.
  `Keep&Integrate` = connect to the existing tool; `Migrate→*` = retire the existing tool
  and replace with an external SaaS (`Buy`) or a **named catalog component** (`Build`, run
  in Databricks); `New→*` = a domain with no current tool. Justification cites the
  keep-vs-migrate and build-vs-buy logic (§14.3/§14.4 — don't rebuild commodities;
  integrate free incumbents; build the vertical/consolidation gaps).
- `ComponentConfig` — for Engine A: `{ objects[], fields[], views[], labels }`; for Engine
  B: `{ source, sample_dataset, delta_target, dashboard_ref }`.

### 3.5 Required current-systems inventory (un-skippable)

The `OrgProfile` must capture what the org runs **today**, by fixed category, so nothing is
glossed over — this is the substrate the keep/build-vs-buy/build blueprint reasons about:

- **Required categories:** email, docs/productivity, financial/accounting, CRM/constituent,
  fundraising/donations, plus the remaining §14.1 domains (grants, program/case, volunteer,
  events, comms, back-office, vertical/operational).
- **Per category:** `{ category, has_system: yes|no, system_name?, keep_intent?: keep|open-to-change|unsure }`.
- **Enforcement (UI checklist gates the stage):** the wizard shows a live **Current
  Systems checklist**. The agent elicits entries conversationally and the intake pre-fills
  what it can, but the **Profile stage cannot complete until every required category is
  filled or explicitly marked "none."** This is a *hard* gate in the shell (the exception
  to the otherwise soft, playbook-level stage enforcement), chosen specifically because
  these fields must not be skipped. It stays conversational — the checklist reflects what
  the dialog captured; it is not a data-entry form up front.

---

## 4. Data flow

1. **Intake (optional):** user pastes text / uploads a file / provides links → `/ingest`
   → backend extracts text (file parsing via Databricks `ai_parse_document` for
   PDFs; best-effort fetch for links) → returns a **context pre-fill** the agent uses to
   skip already-answered questions.
2. **Discovery:** React ⇄ `/chat` ⇄ in-process agent (Discovery Playbook). Agent asks
   segmenting questions, emitting `OrgProfile` → `DomainRelevance` → `Blueprint`. Each
   emitted artifact appears as an inspectable status-bar node; `Blueprint` renders on the
   result surface with Buy vs Run-in-DBX tags.
3. **Configure Donor Mgmt (Engine A):** from the blueprint, user opens the Donor Management
   component → config wizard (Configuration Playbook over the donor schema) → `ComponentConfig`
   → the **donor app renders live** from that config on Lakebase.
4. **Set up Finance Reporting (Engine B):** user opens the reporting component → short
   wizard confirms source → backend loads the **sample QuickBooks export** into a Delta
   table → surfaces the **Lakeview dashboard**.

---

## 5. Persistence (minimal)

- **Lakebase (Postgres):** donor app data (`donors`, `gifts`, with a `custom` JSONB column
  for agent-added fields); optionally the current session's artifacts (so a refresh
  survives). Single logical tenant for the demo.
- **Delta/UC:** the sample QuickBooks dataset loaded by Engine B.
- No cross-session durable history, no per-user store (deferred to multi-tenant slice).

---

## 6. Identity & tenancy (seam only)

The demo runs as the Databricks App's default identity (whoever is logged in); a single
implicit tenant. All tenant-scoped state (donor data, configs) is keyed by a `tenant_id`
that is **hard-coded to one value** for the demo but present in the schema, so multi-tenancy
can evolve without reshaping storage. apx-agent's OBO identity passthrough is available for
future UC-grant-scoped tools but unused in the demo.

---

## 7. Error handling

- **Link fetch** (intake): best-effort; on timeout/failure, skip silently and tell the user
  which links couldn't be read — never block discovery.
- **Agent non-conformant output:** validate each artifact against its schema; on failure,
  re-prompt the agent once with the schema, then surface a friendly "let's continue"
  fallback rather than crashing the wizard.
- **Dashboard setup:** if Lakeview creation fails, fall back to a static rendering of the
  same query result so the demo still shows a chart.
- **Lakebase unavailable:** donor app degrades to an in-memory store for the session.

---

## 8. Testing strategy

- **Artifact schema validation** (unit): every playbook stage's output validates against
  its JSON schema; golden examples for Urban Gleaners.
- **Backend contract tests:** `/chat`, `/ingest`, `/donor/*`, `/dashboard` against a
  stubbed in-process agent (deterministic canned artifacts) — no live model needed in CI.
- **Ingestion test:** sample QuickBooks file → Delta table row counts / schema assertions.
- **Frontend:** a smoke test that the wizard renders nodes as artifacts arrive and the
  donor app renders from a config fixture.
- **One scripted end-to-end demo rehearsal** against the live model as manual acceptance.

TDD applies to the backend contract and artifact schemas (the deterministic parts); the
agent's conversational quality is validated by rehearsal, not unit tests.

---

## 9. Deployment (Databricks Apps)

- Single app: `app.yaml` runs the FastAPI process; React is built to static assets served
  by FastAPI.
- Resources: a Lakebase (Postgres) instance; a SQL warehouse / UC schema for the Delta
  table + Lakeview dashboard; a Foundation Model serving endpoint.
- Secrets/config via app env (model endpoint, Lakebase connection, warehouse id).
- **Note:** the project lives under a Google-Drive-synced path; exclude `node_modules`,
  `.venv`, and build output from sync/git to avoid churn.

---

## 10. Rough build order (fits the 2 days)

**Immediate focus (first increment):** the **spine** — a basic React chat/wizard interface
plus the discovery agent loop — built as a tight iteration harness for **tuning the
apx-agent discovery agent** (playbook, grounding, required-systems elicitation, blueprint
quality). Everything else follows once the agent feels right.

1. Project scaffold: single Databricks App (FastAPI + Vite/React), local run, auth to
   workspace + model endpoint.
2. Component catalog data file (specs + 2 full teaser schemas) + the research brief wired
   as agent grounding.
3. Discovery Playbook + artifact schemas; `/chat`; **Current Systems checklist gate**;
   blueprint renders (spine first). ← *primary iteration surface for agent tuning*
4. Generic wizard shell: conversation + status bar + artifact inspector + blueprint surface.
5. Intake (`/ingest`): text + file parse + best-effort link fetch → pre-fill.
6. Engine A: donor schema-driven CRUD on Lakebase + Configuration Playbook → live donor app.
7. Engine B: sample QuickBooks → Delta + Lakeview dashboard surface.
8. Deploy to Databricks Apps; rehearse the end-to-end demo.

Spine (1–4) before teasers (6–7); if time runs short, one teaser can drop without breaking
the story.

---

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| apx-agent in-process invocation rougher than documented | Validate `run_once`/Python API in step 1 (a spike); fall back to calling apx-agent's local FastAPI on localhost if needed. |
| Databricks Apps deploy friction eats the timeline | Keep it local-first through step 7; deploy is step 8, not a prerequisite. |
| Generic app engine balloons | Hard-cap to 1–2 objects + 1 custom field; it's a teaser, not a builder. |
| Link auto-fetch flaky from the app | Best-effort, graceful degradation; text+file are the reliable paths. |
| Lakeview embedding limitations in an App | Link out to the dashboard if iframe embedding is restricted; static-chart fallback. |
| Model output drift from schemas | Schema validation + one re-prompt + friendly fallback. |

---

## 12. Future slices (post-demo)

2. Build out the Component Catalog (more Engine-A/B configurations, richer schemas).
3. Full per-component configuration wizards + editing/revisiting stages.
4. Provisioning: instantiate configured components for a tenant end-to-end.
5. Multi-tenancy & identity hardening (real tenant model, auth, PLG activation
   instrumentation).
