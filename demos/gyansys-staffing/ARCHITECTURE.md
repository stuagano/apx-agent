# GyanSys Staffing Coworker — Architecture & Deep-Dive Talking Points

This document is the technical companion to the runnable phase-1 demo. It maps
each customer ask to what was actually built, then carries the conversation
forward into the doc-only sections (predictive, cost, platform boundaries) that
shape a phase-2 discussion. The demo itself — a read-only `CoworkerAgent` on
`fe-stable` that matches Salesforce opportunities to Replicon people by skill
similarity — is the concrete reference for everything below.

The framing for the deep dive is simple: GyanSys wants practice leadership to
match the right people to the right work, see stalled pipeline, and understand
staffing bandwidth across regions — **without** buying every manager a
Salesforce seat. Salesforce stays the system of record. Phase 1 never writes
back. Everything here is read-only.

---

## 1. Phase-1 Data Flow

The phase-1 build is a thin, legible pipeline: two source systems land in Unity
Catalog as Delta tables, one of those tables is indexed for vector similarity,
and a single `CoworkerAgent` reads both through governed tools and serves a
Databricks App to management users.

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

**What is real in the demo, and what is the seam.** The agent, the Vector
Search index, the grounded SQL path, and the App are all real and run live on
`fe-stable`. The *ingestion* on the left edge is the only synthetic part: a
deterministic generator (`generate_data.py`, seeded with `42` and a fixed
`REFERENCE_DATE = 2026-07-01`) produces ~75 opportunities and ~200 people, and
`load_to_uc.py` writes them into `gyansys_demo.staffing.salesforce_opportunities`
and `gyansys_demo.staffing.replicon_people` through a serverless SQL warehouse.
Using synthetic seed data is a deliberate phase-1 choice — it lets us plant the
demo moments (a handful of obviously-stalled high-value opportunities; India
deliberately tight on Databricks/PySpark) and keeps the build focused on the
agent rather than connector plumbing.

**Where live ingestion plugs in.** The two `(...)` labels on the source edges
are exactly the integration points for a production build, and nothing
downstream changes when they are swapped in:

- **Salesforce → Lakeflow Connect.** The synthetic seed of
  `salesforce_opportunities` is replaced by a managed Lakeflow Connect ingestion
  pipeline that lands opportunity records into the same UC table on a schedule.
  Because the agent reads UC, not Salesforce, the source swap is invisible to the
  agent, the index, and the App.
- **Replicon → batch sync.** People, skills, certifications, and availability are
  refreshed by a periodic batch sync into `replicon_people`. The derived
  `skill_profile` column (`title | skills | certifications`) is recomputed on
  load, and the table already has Change Data Feed enabled
  (`delta.enableChangeDataFeed = true`) so the delta-sync Vector Search index
  picks up changes incrementally.

The takeaway for the customer: the demo's data flow *is* the production data
flow. Only the two ingestion edges change, and they change behind a stable UC
contract.

---

## 2. Databricks Apps Fit

The deployed artifact is a **Databricks App** that serves the `CoworkerAgent` as
an internal, read-only front end. This is where the commercial story lives, not
just the technical one.

**The license-cost win.** Today, a practice lead who wants to know "is this
opportunity stalled, and who could we put on it?" either pings someone who has a
Salesforce seat or gets cut out of the loop entirely. Salesforce licenses are
expensive and are provisioned for sellers, not for the delivery and management
layer. The App lets management *read* pipeline and staffing posture through a
governed natural-language interface without consuming a Salesforce seat each.
Salesforce remains the system of record and the place sellers transact; the App
is a read-only lens over a UC copy of that data, broadened to the many managers
who need to *see* but never need to *edit*.

**Why an App and not a notebook or a BI dashboard.** The audience is
business and delivery leaders across four regions — not engineers and not
dashboard authors. A Databricks App gives them a chat surface that speaks in
management terms (fit, bandwidth, pipeline health) and runs inside the same
Databricks identity, governance, and audit perimeter as the data. There is no
separate app server to operate, no separate auth system to integrate, and no
data leaving the platform. Unity Catalog permissions and the App's own access
controls are the access model; we inherit them rather than reinventing them.

**Deployment note (phase-1 reality).** App deploys to `fe-stable` have hit a
known bundle-state mismatch (bundle state pointing at a different workspace,
producing a workspace_id / home-directory mismatch). The implementation plan
treats the deployed App as the goal and a documented local-only run as the
fallback if that bundle issue blocks the timeline — the local dev UI exercises
the exact same agent, so the deep-dive story holds either way.

---

## 3. Genie & Grounding

The single most important technical claim in this demo is **anti-hallucination
by construction**, and it rests on two layers: schema grounding and vector
retrieval.

**Schema grounding (the agent knows the real columns before the first
question).** The `CoworkerAgent` is a `DataAgent`-family agent scaffolded
against `gyansys_demo.staffing`. Its schema is baked at scaffold time (the
`.apx/schema.json` grounding artifact), so the model knows the actual tables and
columns — `stage`, `amount`, `last_activity_date`, `stall_reason`,
`availability_pct`, `region`, and the rest — *before* it generates a single
query. It does not run `SHOW TABLES` to discover structure at runtime, and it
does not invent fields. When the agent answers "which opportunities are
stalled?", it is reasoning over `stage ∈ {open stages}` and
`last_activity_date` older than 30 days relative to `2026-07-01`, then citing the
literal `stall_reason` string — real columns, real values, no fabrication. The
agent instructions reinforce this: never invent columns, never claim to modify
Salesforce.

**Vector retrieval (the matching layer that would otherwise hallucinate).** The
hardest place for an LLM to be honest is the *match*: "who fits this
opportunity?" There is no shared key between Salesforce opportunities and
Replicon people — the two systems link only through the skills taxonomy. Rather
than let the model guess at fit, the demo grounds the match in a Databricks
Vector Search index over the people skill-profiles
(`replicon_people_index`, managed embeddings via `databricks-gte-large-en`,
primary key `person_id`). The agent embeds the opportunity's
`required_role + required_skills`, runs a nearest-neighbor query against the
index, and only then ranks the returned candidates by `availability_pct` and
`region`. The retrieval is real cosine similarity over real embeddings, so the
recommended people genuinely exist and genuinely carry the relevant skills.

**Where Genie fits.** For open-ended natural-language exploration beyond the
baked tools, a Genie space over `gyansys_demo.staffing` can be added as a
`type: genie` tool on the agent (this is the optional task in the plan). Genie
broadens the question surface — ad-hoc "what's our pipeline by stage in South
America?" style questions — while the schema-grounded SQL tools and the vector
index remain the backbone for the scripted demo moments. Genie is additive, not
a replacement for the grounding: it is the free-form complement to the precise,
pre-grounded path.

---

## 4. Predictive (Doc-Only)

No predictive model is built in phase 1. This section describes where one would
slot in, because it is a natural and frequently-asked phase-2 extension.

The same UC tables that ground today's descriptive answers are the training
substrate for tomorrow's predictive ones. Two models map cleanly onto the
existing schema:

- **Stall-risk model.** Over *historical* opportunities (close dates, stage
  transitions, activity cadence, amount, region, required skills), an MLflow
  model would learn the probability that an open opportunity stalls. The demo
  already exposes the descriptive version — "this opp has had no activity for 45
  days" — and the predictive version turns that into "this opp has a 70% chance
  of stalling in the next 30 days, here's why." It would register in Unity
  Catalog, serve behind a Model Serving endpoint, and surface to the agent as one
  more read-only tool (`predict_stall_risk`).
- **Profitability / margin model.** `replicon_people.cost_rate` and opportunity
  `amount` are already in the schema precisely so this story is available.
  A profitability model would score a *staffing recommendation* — not just "who
  fits" but "who fits at what blended margin," helping leadership weigh a
  cheaper-but-available bench resource against a higher-rate specialist.

The architectural point: predictive is a clean addition, not a redesign. The
ingestion, UC governance, and agent surface are unchanged; a predictive model is
a new MLflow asset and a new tool. Phase 1 deliberately stops at descriptive +
retrieval so the demo is honest about what runs live.

---

## 5. Cost

The phase-1 stack is deliberately cheap, and the cost story is one of the
strongest commercial arguments.

**What actually consumes.**
- **Serverless SQL warehouse** — used for loading and for the agent's grounded
  SQL queries. Serverless means we pay for query time, not idle capacity; the
  workload here (a handful of aggregations over ~75 + ~200 rows) is negligible.
- **Vector Search index** — a delta-sync index over ~200 rows with managed
  embeddings (`databricks-gte-large-en`). At this scale the index and its
  embedding refreshes are trivially inexpensive; this is not a billion-vector
  workload, it is a small reference table.
- **Foundation Model API** — the agent LLM (`databricks-claude-sonnet-4-6`),
  billed per token on the questions management actually asks.
- **Databricks App** — the serving surface for the read-only front end.

**Quote it against the alternatives.**
- **vs. Salesforce license expansion.** The whole point of the App is to give
  read access to many managers without a Salesforce seat each. Even a modest
  number of additional Salesforce seats dwarfs the serverless + small-index +
  per-token cost of this demo. The cost comparison is the license-cost win from
  Section 2, made quantitative.
- **vs. a traditional hosted app.** A bespoke internal app would mean standing up
  and operating an app server, a database, an auth layer, and a data-sync job —
  ongoing fixed cost and engineering toil. Databricks Apps + UC + serverless
  collapse that into managed, consumption-priced services with governance
  inherited from the platform.

The headline: a ~200-row vector index plus serverless query and per-token LLM is
a rounding error next to either incremental Salesforce licenses or a
self-operated hosted application.

---

## 6. Platform Boundaries (Doc-Only)

Phase 1 is scoped to an internal, read-only, single-tenant analytics surface.
This section names the boundaries the customer will reasonably probe, and
positions each as a phase-2+ evaluation rather than a phase-1 gap. None of these
are built; calling them out keeps the deep dive honest.

- **External / vendor / attorney access.** The phase-1 audience is internal
  management inside the GyanSys identity perimeter. Extending read access to
  outside parties (vendors, subcontractors, outside counsel) raises a different
  set of access-control, data-scoping, and audit requirements — share only the
  rows a given external party may see — and is a deliberate phase-2 evaluation,
  not a phase-1 capability.
- **Multi-tenant.** The demo serves one organization's data in one UC schema.
  Serving multiple isolated tenants (e.g., per-business-unit or per-client
  partitions with hard isolation) is an architecture decision in its own right
  and is out of phase-1 scope.
- **Transactional use cases.** This is an analytical, read-only lens. The moment
  the requirement becomes *write* — update an opportunity, assign a person,
  persist a staffing decision back to a system of record — the right shape is a
  transactional application (the React / Python / Postgres pattern, with Lakebase
  as the operational store) rather than a read-only coworker over UC. That is a
  distinct phase-2+ build, and it is explicitly *not* what phase 1 promises:
  phase 1 never writes back, and Salesforce stays the system of record.

Stating these boundaries is itself part of the value: it shows the platform has a
clear answer for each ("here's the Databricks shape when you get there") without
overpromising what the phase-1 demo does today.

---

## 7. Demo Script

The scripted deep-dive flow maps each customer ask to a concrete agent action.
The questions below are the agent's three example prompts plus the two
platform-level talking points. The **Expected answer** column describes what a
correct, grounded response looks like given the planted synthetic data — the
agent has not yet been run live as part of this document.

_Verified transcript appended after the local run (plan Task 6)._

| Customer ask | Agent action | Expected answer |
|---|---|---|
| "Match the right people to the right work" — *"Who are the best-fit available people for the highest-value stalled opportunity?"* | SQL tool finds the highest-`amount` opportunity in an open stage with `last_activity_date` > 30 days before 2026-07-01; vector tool embeds its `required_role + required_skills` and queries `replicon_people_index`; candidates ranked by `availability_pct` and `region`. | Names the specific high-value stalled opportunity (Proposal stage, six-figure `amount`) and lists specific people by name with their region, key matching skills, and availability %, preferring higher-availability and region-aligned candidates. Both the vector tool and the SQL tool are exercised. |
| "Which opportunities are stalled, and why?" | Grounded SQL over `salesforce_opportunities`: open stage (Prospecting / Qualification / Proposal / Negotiation) AND `last_activity_date` older than 30 days before 2026-07-01; selects `stall_reason`. | Lists the planted stalled opportunities (Proposal stage, idle > 30 days, high `amount`) each with its concrete `stall_reason` — e.g. "Awaiting customer security review", "Budget approval pending", "Champion left the account", "Stuck on legal redlines". No invented opportunities. |
| "Where's our bandwidth?" — *"How much availability do we have for Databricks work in India?"* | Vector tool and/or SQL filter on `region = 'India'` and Databricks/PySpark skills; aggregates `availability_pct`. | Reports the planted India-on-Databricks scarcity: the India people who carry Databricks/PySpark have low availability (single-digit to ~20%), so bandwidth for Databricks work in India is tight — the deliberate bandwidth story. |
| "Broaden access without more SF licenses" | (No agent call — the Databricks App itself is the answer.) | Demonstrates management reading pipeline and staffing posture through the App without consuming a Salesforce seat each — the license-cost win from Section 2. |
| "Grounding / no hallucination" | Show the baked `.apx/schema.json` grounding artifact. | The agent cites real columns from `salesforce_opportunities` and `replicon_people` and never invents fields, because the schema is grounded at scaffold time (Section 3). |

After the local run in plan Task 6, the three agent questions above will have
their actual responses recorded (trimmed) under a "Verified demo answers"
heading in `README.md`, and the verified transcript will be reflected here.
