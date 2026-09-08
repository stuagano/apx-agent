# Agentic Nonprofit Suite — Discovery Spine and Future Engines

*Design spec — 2026-08-27; status corrected 2026-09-09. The discovery spine shipped in
PR #693. Configuration, donor-management, and finance-reporting engines remain roadmap.*

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

## 2. Current scope and status

The repository currently delivers a deployed discovery experience through a native APX
agent and the generated TypeScript AppKit host. The original two-engine prototype was not
completed and remains future product scope.

### 2.1 Shipped discovery spine

- Native APX discovery agent with playbook, catalog, skill, and nonprofit research brief
  grounding.
- Generated TypeScript AppKit host serving the React wizard and
  `/api/agents/chat` with real AppKit threads and telemetry.
- Typed `OrgProfile`, `DomainRelevance`, and `Blueprint` artifacts with an inspectable
  progress rail, current-systems checklist, artifact inspector, and blueprint surface.
- Seven catalog entries describing possible Databricks-hosted components.
- URL intake, browser-readable text-file intake, and filename annotations for binary
  files.
- Databricks Apps bundle configuration for the AppKit host, model endpoint, SQL warehouse,
  and MLflow telemetry resources.

### 2.2 Actionable roadmap

- Add full configuration schemas for the donor-management and finance-reporting catalog
  entries and emit a validated `ComponentConfig` artifact.
- Add a per-component configuration wizard.
- Build the minimal schema-driven Donor and Gift application on Lakebase.
- Load a sample QuickBooks export into Delta and surface a real AI/BI dashboard.
- Add pasted-text intake, server-side document parsing, and backend context prefill.
- Complete all twelve current-system categories before `OrgProfile` can pass the hard
  gate; the shipped gate currently requires five core categories.
- Re-prompt once when an agent artifact fails schema validation before surfacing the
  rejection.

### 2.3 The honesty line

The discovery agent, AppKit conversation, three discovery artifacts, and blueprint UI are
real. The configuration wizard, generated component configuration, Lakebase donor data,
QuickBooks-to-Delta ingestion, and AI/BI dashboard are not implemented yet.

---

## 3. Architecture

### 3.1 Shipped topology — generated AppKit host

```
React discovery wizard
  └─ POST /api/agents/chat
       └─ generated APX TypeScript AppKit host
            ├─ native APX discovery-agent declaration
            ├─ Databricks foundation model and governed Python tools
            └─ MLflow/AppKit traces, metrics, and logs
```

The original custom FastAPI `/chat`, `/ingest`, `/donor/*`, and `/dashboard` topology was
superseded. TypeScript remains internal runtime plumbing generated from the Python agent
declaration.

### 3.2 Units and interfaces

| Unit | Status | Responsibility |
|---|---|---|
| **Discovery shell (React)** | Shipped | Conversation, progress, artifact inspection, current-systems gate, and blueprint surface |
| **Generated AppKit host** | Shipped | Static client, `/api/agents/chat`, threads, governed tool dispatch, and telemetry |
| **Discovery agent (apx-agent)** | Shipped | Follow the discovery playbook and emit the three typed discovery artifacts |
| **Component catalog** | Partial | Seven component outlines; teaser configuration schemas remain future work |
| **Configuration wizard** | Future | Fill and validate one catalog component's `ComponentConfig` |
| **Donor app engine** | Future | Render Donor and Gift CRUD from configuration using Lakebase |
| **Ingest and BI engine** | Future | Load sample finance data into Delta and surface an AI/BI dashboard |

The Python declaration remains the product-facing source of truth; the generated AppKit
host is runtime plumbing.

### 3.3 Agent design (apx-agent)

- A single `LlmAgent` (Databricks Foundation Model endpoint — prefer a strong
  instruction-following model, e.g. Claude on Databricks if available; else Llama).
  Model id pinned at setup.
- **Grounding:** the research brief (as context/knowledge) + the component catalog file.
- **Shipped playbook:** Discovery stages move through **Org Profile** → **Domain
  Relevance** → **Suite Blueprint**.
- **Future playbook:** Component configuration is parameterized by a catalog schema and
  emits a validated **Component Config** artifact.
- **Enforcement:** artifact parsing and the five-category current-systems gate are hard
  client checks. The remaining conversational progression is prompt-driven.
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
- `ComponentConfig` (**future**) — for Engine A:
  `{ objects[], fields[], views[], labels }`; for Engine B:
  `{ source, sample_dataset, delta_target, dashboard_ref }`.

### 3.5 Required current-systems inventory (un-skippable)

The `OrgProfile` must capture what the org runs **today**, by fixed category, so nothing is
glossed over — this is the substrate the keep/build-vs-buy/build blueprint reasons about:

- **Required categories:** email, docs/productivity, financial/accounting, CRM/constituent,
  fundraising/donations, plus the remaining §14.1 domains (grants, program/case, volunteer,
  events, comms, back-office, vertical/operational).
- **Per category:** `{ category, has_system: yes|no, system_name?, keep_intent?: keep|open-to-change|unsure }`.
- **Current enforcement:** the live checklist requires email, docs/productivity,
  financial/accounting, CRM/constituent, and fundraising/donations before the profile can
  complete.
- **Future enforcement:** extend that hard gate to all twelve categories above. The
  checklist remains conversational rather than becoming an up-front data-entry form.

---

## 4. Data flow

1. **Shipped intake:** the client includes an organization URL and browser-readable file
   text in the first discovery turn. Binary files contribute their filenames. Pasted text,
   server-side document parsing, and a separate `/ingest` endpoint remain future work.
2. **Shipped discovery:** React ⇄ `/api/agents/chat` ⇄ generated AppKit host ⇄ native APX
   discovery agent. `OrgProfile`, `DomainRelevance`, and `Blueprint` artifacts appear in
   the progress rail and result surface.
3. **Future donor configuration:** open Donor Management from the blueprint, emit a
   `ComponentConfig`, and render Donor and Gift CRUD from it on Lakebase.
4. **Future finance configuration:** confirm the sample source, load it into Delta, and
   surface an AI/BI dashboard or link.

---

## 5. Future engine persistence

The shipped discovery spine relies on AppKit threads and does not provision product data
stores. Engine A will use Lakebase for `donors`, `gifts`, and a `custom` JSONB field.
Engine B will use a Delta table for the sample QuickBooks dataset. Both remain scoped to a
single demo tenant until the multi-tenant control plane exists.

---

## 6. Identity & tenancy (seam only)

The discovery app uses the generated AppKit host and APX identity contracts. Future
engine data is scoped to one implicit demo tenant; its schemas should include a stable
`tenant_id` so a later multi-tenant control plane does not require reshaping storage.

---

## 7. Error handling

- **Shipped artifact handling:** validate each artifact in the client and display rejected
  artifacts without crashing the wizard.
- **Future artifact recovery:** re-prompt once with the schema before displaying a
  rejection.
- **Future dashboard setup:** if dashboard creation fails, render the same aggregate data
  as a static chart.
- **Future Lakebase fallback:** use an in-memory session store when Lakebase is unavailable
  during a local demo.

---

## 8. Testing strategy

- **Shipped:** native-agent declaration and grounding tests, AppKit client contract tests,
  artifact parsing, current-systems gate, progress shell, and deployment configuration.
- **Future configuration engine:** schema-validation fixtures for both teaser configs.
- **Future donor engine:** CRUD contract tests and configuration-driven rendering tests.
- **Future finance engine:** sample-file schema and row-count checks plus dashboard
  reference validation.
- **Acceptance:** one scripted end-to-end rehearsal against the live model and explicitly
  selected Databricks profile.

TDD applies to the backend contract and artifact schemas (the deterministic parts); the
agent's conversational quality is validated by rehearsal, not unit tests.

---

## 9. Deployment

- **Shipped:** `databricks.yml` stages the React client, APX wheel, example wheel, and
  generated AppKit host. It declares model endpoint, SQL warehouse, and MLflow experiment
  resources.
- **Future:** add Lakebase and any additional UC/dashboard resources only when implementing
  the two engines. Deployment and validation must always pass an explicit CLI profile.

---

## 10. Delivery order

- [x] Native APX agent and generated AppKit host.
- [x] Grounded discovery playbook, three discovery artifact types, and catalog outlines.
- [x] React conversation, progress, artifact inspection, systems checklist, and blueprint.
- [x] Databricks Apps deployment configuration for the discovery spine.
- [ ] Complete all-category profile gating and richer intake.
- [ ] Add teaser configuration schemas and the configuration playbook.
- [ ] Build Engine A: configuration-driven Donor and Gift CRUD on Lakebase.
- [ ] Build Engine B: sample QuickBooks ingestion into Delta plus an AI/BI dashboard.
- [ ] Rehearse the complete discovery-to-configured-engine demo.

---

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Generated AppKit contract changes | Keep the Python declaration authoritative and verify the generated host through APX deployment tests. |
| Databricks resource setup slows engine work | Keep engine contracts locally testable; provision Lakebase, Delta, and dashboard resources only for integration validation. |
| Generic app engine balloons | Hard-cap to 1–2 objects + 1 custom field; it's a teaser, not a builder. |
| Link auto-fetch flaky from the app | Best-effort, graceful degradation; text+file are the reliable paths. |
| Lakeview embedding limitations in an App | Link out to the dashboard if iframe embedding is restricted; static-chart fallback. |
| Model output drift from schemas | Schema validation + one re-prompt + friendly fallback. |

---

## 12. Later slices

1. Build out the Component Catalog with more Engine A and Engine B configurations.
2. Add editing and revisiting for completed wizard stages.
3. Provision configured components for a tenant end to end.
4. Add multi-tenancy and identity hardening: a real tenant model, auth, and PLG activation
   instrumentation.
