# Coworker Agent Use Cases — Joining Disparate Systems

The core value of the `CoworkerAgent` class is **the join**: two systems of record
that each own half the truth, joined on a business entity, answering a question
neither system can answer alone.

## What the CoworkerAgent actually is

`CoworkerAgent` **is a** `DataAgent` (subclass, not a wrapper — see
`python/src/apx_agent/coworker.py`). It adds three identity knobs and one
memory knob on top of `DataAgent`:

1. **`persona`** — a plain string woven into the grounded instructions the
   `DataAgent` already builds from the schema. There is no separate
   `PersonalityConfig` class.
2. **`join_key`** — the business entity linking the two source systems
   (e.g. `"employee ID"`, `"opportunity ID"`). Folded into the objective so
   the agent knows which field to join on across landed tables.
3. **`objective`** — what this agent is designed to do. Combined with
   `join_key` when both are given.
4. **`memory`** — a one-word tier knob: `"off"` (default) / `"inmemory"` /
   `"persistent"` (Lakebase, with generated host/database/embedding-model
   defaults) / `"managed"` (UC managed memory). It normalizes into
   `MemoryBackendConfig` + `SessionBackendConfig` carried as declared config;
   the framework's finalize/serve path does the wiring, so construction needs
   no `ws`. Typing `memory="lakebase"` literally deliberately raises — that's
   for fully custom pgvector connection details via explicit
   `[tool.apx.agent.memory]` / `[tool.apx.agent.session]` blocks, distinct
   from `"persistent"`'s generated defaults.

```python
agent = CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
    join_key="employee ID",
    objective="surface mismatches between hours worked and paychecks issued",
    memory="persistent",
)
```

The declarative surface is **TOML, not YAML**: `[tool.apx.agent]` in
`pyproject.toml`. Two ways to get a coworker:

- **Code-first** — `apx-agent agents scaffold my-coworker --template coworker`
  generates a `my-coworker/` project with an `agent.py` holding the one-liner
  above (or describe it in plain English with `apx-agent generate "..."`
  instead).
- **Config-first (template-as-config)** — `CoworkerTemplate`
  (`name = "coworker"`) exposes a pydantic `Spec` (`catalog`, `schema`,
  `warehouse_id`, `persona`, `join_key`, `objective`, `memory`, `genie_space`,
  `vector_index`, `include_functions`); `build(spec)` constructs the
  `CoworkerAgent`. The Spec is the entire declarative surface.

**Mental model:** `CoworkerAgent = DataAgent + persona + join_key + objective` —
pre-grounded in the schema (it already knows the tables/columns), knows which
field links the two source systems, and remembers across turns (facts +
session). Default memory works with zero infra; Lakebase is an upgrade, never
a prerequisite.

## The outline and the colors

The template is the **outline**; the data **fills in the colors**. The
template never contains data — it *points* at it. The Spec holds the shape of
the coworker (persona, memory, which tools); `catalog`/`schema` are a
reference to a UC schema that lives next to, and separate from, the template.

That separation is the whole GTM story: the two source systems land in the
lakehouse first (Lakeflow Connect or whatever ingestion you have — Kronos and
Workday tables side by side in one UC schema), and the coworker is grounded
over the *joined landing zone*. The join the agent reasons about is a join the
lakehouse already made physically possible.

So every use case below is **the same outline, different colors**:

```toml
# Payroll coworker — Kronos × Workday
[tool.apx.agent]
template   = "coworker"
catalog    = "main"
schema     = "payroll"        # Kronos + Workday landed tables
persona    = "a payroll operations analyst"
join_key   = "employee ID"
objective  = "surface mismatches between hours worked and paychecks issued"
memory     = "persistent"
```

```toml
# Quote-to-Cash coworker — Salesforce × NetSuite: SAME template
[tool.apx.agent]
template   = "coworker"
catalog    = "main"
schema     = "revops"         # Salesforce + NetSuite landed tables
persona    = "a revenue operations analyst"
join_key   = "opportunity ID"
objective  = "identify revenue leakage between closed deals and invoiced amounts"
memory     = "persistent"
```

Nothing else changes. One template, seven coworkers — the Spec fields are the
blanks, the customer's schema and persona are the fill. That's why the
use-case list below can grow without any new code: a new use case is a new
pair of landed systems plus a one-paragraph persona, not a new agent class.

Each use case below has a **runnable declaration** under
[`docs/coworkers/`](../coworkers/) — a complete YAML spec grounded on a governed
`sql` tool (bound to a warehouse, run under per-call user identity so UC grants
apply). Point it at your schema, set the env vars, and it serves.

**The pattern:** the join key is a business entity (employee, deal, asset,
shipment, encounter), each system is authoritative for half the record, and the
expensive human workflow today is a person doing the join manually — tabbing
between two screens.

## Reference example

### Payroll Agent — Kronos × Workday

- **Runnable:** [`coworkers/payroll.yaml`](../coworkers/payroll.yaml)
- **Join key:** employee ID
- **System A (Kronos):** time and attendance — what hours were actually worked
- **System B (Workday):** HR and payroll — what the employee should be paid
- **Question only the join answers:** "Why doesn't this paycheck match the
  hours worked, and which punches or pay rules caused the discrepancy?"

## Six more

### 1. Quote-to-Cash Agent — Salesforce × NetSuite

- **Runnable:** [`coworkers/quote-to-cash.yaml`](../coworkers/quote-to-cash.yaml)
- **Join key:** account / opportunity → invoice
- **Salesforce:** what was sold and on what terms
- **NetSuite:** what was billed and collected
- **Question:** "Did the deal we closed actually invoice at the negotiated
  discount, and why is this customer 60 days past due on a contract Sales
  thinks is healthy?" Revenue leakage lives exactly in that gap.

### 2. Onboarding/Offboarding Agent — Workday × Okta (or AD)

- **Runnable:** [`coworkers/onboarding.yaml`](../coworkers/onboarding.yaml)
- **Join key:** employee ID
- **Workday:** employment status, start/term dates
- **Okta/AD:** what access actually exists
- **Question:** "Which day-one new hires have no accounts — and which
  terminated contractors still have access?" The second one is a
  compliance/audit story, not just convenience.

### 3. Warranty & Entitlement Agent — ServiceNow × SAP

- **Runnable:** [`coworkers/warranty-entitlement.yaml`](../coworkers/warranty-entitlement.yaml)
- **Join key:** customer / asset serial number
- **ServiceNow:** what broke, what the customer is asking for
- **SAP:** contract, warranty terms, parts inventory
- **Question:** "Is this repair covered, do we have the part, and what should
  the customer actually pay?" Today that's a support rep tabbing between two
  screens.

### 4. Order-Status Agent — Oracle ERP × project44/FourKites (TMS)

- **Runnable:** [`coworkers/order-status.yaml`](../coworkers/order-status.yaml)
- **Join key:** PO / shipment number
- **Oracle ERP:** what was ordered and invoiced
- **TMS:** where the freight physically is
- **Question:** "Where's my order, will it hit the dock date, and does the
  carrier invoice match the rate on the PO?" Three-way match plus live
  tracking in one conversation.

### 5. Claims Integrity Agent — Epic × claims/clearinghouse

- **Runnable:** [`coworkers/claims-integrity.yaml`](../coworkers/claims-integrity.yaml)
- **Join key:** patient encounter
- **Epic (EHR):** what care was documented
- **Claims system:** what was coded, submitted, and denied
- **Question:** "Why was this claim denied, and is the supporting
  documentation actually in the chart?" Denial management is a huge labor
  line item and it's purely a cross-system reconciliation problem.

### 6. Financial Reporting Agent — Billing × General Ledger

- **Runnable:** [`coworkers/financial-reporting.yaml`](../coworkers/financial-reporting.yaml)
- **Join key:** invoice ID
- **Billing system:** what was invoiced to customers
- **General ledger:** what revenue was posted to the books
- **Question:** "Reconcile billed revenue against the GL for last close — which
  invoices never posted, and where are the dollar variances?" This is the
  governed replacement for a reporting team hand-exporting accounting reports
  to Google Drive: the answer *is* the report, computed live from the source of
  truth, so finance stops consuming stale spreadsheets. (Origin: a real customer
  onboarding thread.)
- **Access model:** two roles. Reporting *writes* the governed tables (scheduled
  job as a service principal); internal consumers like accounting *read* under
  their own identity via the coworker's OBO `sql` tool — writer/reader separation
  by UC grant, not by who holds the share link.
- **Phasing:** the governed table on a schedule is the first win (consumable as
  plain SQL); declaring the coworker on top is a later, optional phase. Land the
  *internal* handoff first — where you control both ends — before touching any
  external/contracted customer's existing file handoff.

## Why this sells

- **Mismatch detection is the product.** The agent isn't summarizing either
  system — it's surfacing disagreements between them: unbilled deals,
  unrevoked access, denied-but-documented claims. Mismatches quantify
  directly in dollars, so the business case slide writes itself.
- **Memory compounds the value.** The `CoworkerAgent`'s persistent memory
  learns account-specific mapping quirks (this customer's PO format, this
  carrier's reference codes), so the join gets cheaper every time.

## Why an agent, and not just a job and a dashboard

The honest objection to reach for first — the one a good data engineer will
raise — is: *"a scheduled transformation writes a governed table, and a
dashboard or Genie space sits on top. Where's the agent even needed?"* Mostly,
they're right, and conceding that is what makes the case for an agent credible.

**The source-of-truth fix is not an agent problem.** The scheduled job that
reconciles billing against the GL and writes the governed table should be
deterministic, tested, versioned SQL running as a job — you do not want an LLM
authoring your books-of-record on a cadence. That job, plus the governed table,
already kills the "right number lives in someone's Excel" problem. A dashboard
or Genie space handles the fixed, repeated views. **That is phase 1, and it is
correct.** If the work were "publish the same monthly summary every close," an
agent would be over-engineering — use the job and the dashboard.

The agent sits *on top of* that pipeline, at the consumption layer, for the last
mile a job and a dashboard can't cover. Three properties of *this* task make it
the right tool there:

- **The question is open-ended, not a fixed view.** A dashboard answers
  questions you knew to ask when you built it. Reconciliation is interrogation —
  "which invoices from the Henderson account never posted last quarter?", "why
  is 4100 short this month?", "show me only the timing differences over $10k."
  Every close surfaces a different question; a dashboard needs a new tile for
  each, the agent answers the ad-hoc one as asked.
- **The output is judgment, not a number.** Summing billings is a `GROUP BY`.
  The value is classifying *why* two systems disagree: a $500 short-post is a
  true error to escalate; a $15k invoice posted one day late is a benign timing
  cut that needs an accrual, not an investigation. A dashboard or raw query
  shows the divergence; it can't tell you which kind it is or what to do about
  it. (In the runnable proof the agent separated all three cases — unposted,
  error, timing — unprompted.)
- **It meets finance in the format they already want.** Accounting often
  resists a dashboard. The agent returns numbers-in-a-table with a plain-language
  explanation — the shape they'd otherwise hand-build in Excel — which they can
  paste into their own sheet. Same figures, computed live, no shared master to
  drift.

**And none of this costs governance.** The agent's `sql` tool runs under the
caller's identity (OBO): accounting sees only what its UC grants allow, every
query is audited, and the writer/reader split (reporting writes as a service
principal, accounting reads) is enforced by grant, not by who holds a share
link. The flexibility is layered *on top of* the same governance the plain-SQL
path gives you, not instead of it.

The one-line version:

> Use a **deterministic job** to produce the governed source of truth. Use an
> **agent** for the open-ended, judgment-heavy interrogation of it that finance
> actually does when the numbers don't tie — the part a dashboard can't
> anticipate and raw SQL can't explain — without giving up per-user governance
> or audit.

That is also the answer to "why not just a job and a dashboard": the job and
the dashboard *are* phase 1 and they're right — but they don't cover the last
mile, and the last mile is where the reconciliation labor lives.
