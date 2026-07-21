# Financial Reporting Coworker — Billing × General Ledger

A governed agent that reconciles billed amounts against posted revenue and
answers finance's reporting questions in natural language. Origin: a real
customer onboarding thread about internal reporting.

- **Join key:** invoice ID
- **Billing system:** what was invoiced to customers
- **General ledger:** what revenue was posted to the books
- **Runnable spec:** [`coworkers/financial-reporting.yaml`](../coworkers/financial-reporting.yaml)
- **Runnable synthetic proof:** [`coworkers/examples/financial-reporting-synthetic.yaml`](../coworkers/examples/financial-reporting-synthetic.yaml)

## The problem it solves

A reporting team hand-exports accounting reports to Google Drive (or a bucket
share); finance consumes them as spreadsheets. There is no source of truth — the
"right number" lives in whoever's copy — and no governance, lineage, or access
audit.

The governed version: publish the reconciliation as governed UC tables (a
scheduled job, not a manual export), then declare this coworker on top. Finance
asks in natural language — *"Reconcile billed revenue against the GL for last
close — which invoices never posted, and where are the dollar variances?"* — and
the coworker runs SQL against those tables and returns the figures, computed live
from the single source of truth. The answer *is* the report, so nobody
hand-uploads and nothing drifts.

## Access model

Two roles, enforced by grant — not by who holds the share link:

- **Reporting *writes*** the governed tables. The scheduled job runs as a service
  principal with grants on the source schemas.
- **Internal consumers (e.g. accounting) *read*,** and only read. They never touch
  the volume or the job. The coworker's `sql` tool runs under the caller's
  identity (OBO), so a reader sees exactly what their UC grants allow, and every
  read is audited.

## Phasing — how this actually lands

1. **Governed table on a schedule** — reporting's first goal is just getting the
   transformation running in Databricks on a cadence. The governed table is the
   win, consumable via plain SQL. This is phase 1, and it stands on its own.
2. **The coworker on top** — a later, optional phase that changes how internal
   customers digest the numbers. Land the *internal* handoff first, where you
   control both ends. For external/contracted customers, the existing file
   handoff may stay as-is.

## Why a coworker and not just Consumer / Genie One

Consumer access surfaces dashboards, Genie agents, and apps — but not
files/volumes, and finance often resists a dashboard format. A coworker answers
the question in the numbers-in-a-table form finance already wants, closing that
gap without a file-share layer inside Databricks.

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

## Proven end-to-end

On 2026-07-21 the coworker was run against synthetic UC tables via
`apx-agent agents run`. It authored SQL against the governed tables under the
caller's identity and correctly separated three cases — an unposted invoice
($5,000, no GL entry), a short-posted amount ($500, flagged as a true error),
and a period-cut timing difference ($15,000 billed 6/30, posted 7/1, flagged as
*not* an error). Reproduce with the synthetic proof spec linked above.

## The runnable declaration

The complete spec. Point it at your catalog/schema/warehouse (set the env
vars), and it serves.

```yaml
name: financial-reporting-coworker
description: >
  Reconciles billed amounts (billing system) against posted revenue (general
  ledger). Answers finance's reporting questions by running SQL against governed
  tables — figures computed live, no hand-exported spreadsheets, no drift.
model: databricks-claude-sonnet-4-6
instructions: >
  You are a financial reporting analyst. Always cite the invoice_id or
  gl_account when surfacing a discrepancy, and state variances in dollar terms.
  Distinguish timing differences (billed this period, posted next) from true
  reconciliation errors. When asked for a report, run SQL against the governed
  tables and return the figures — never quote from a prior export.
examples:
  - "Reconcile billed revenue against the GL for last close — where do they diverge?"
  - "Which invoices were billed but never posted to the ledger this period?"
  - "Show me the revenue variance by account, biggest dollar gaps first"
  - "Give me the monthly billing summary finance usually pulls into their sheet"

template:
  name: coworker
  catalog: $CATALOG
  schema: $SCHEMA
  persona: a financial reporting analyst
  join_key: invoice ID
  objective: >
    Reconcile billed amounts against posted revenue in the general ledger.
    Surface unposted invoices, billing-vs-ledger variances, and timing
    differences, and return the periodic report figures finance consumes today
    as spreadsheets — computed live from the governed source of truth.
  memory: persistent
  warehouse_id: $WAREHOUSE_ID
  include_functions: true

memory:
  type: lakebase
  host: $LAKEBASE_HOST
  database: finrep_coworker
  table_name: $CATALOG.$SCHEMA.apx_finrep_coworker_memory
  embedding_model: databricks-bge-large-en
  embedding_dim: 1024
  auto_create: true
  validate_at_boot: true

session:
  type: lakebase
  host: $LAKEBASE_HOST
  database: finrep_coworker
  table_name: $CATALOG.$SCHEMA.apx_finrep_coworker_sessions
  auto_create: true
  validate_at_boot: true

guardrails:
  injection_detection: true
  blocked_tools: []
  rate_limit: null

# The governed data tool. `sql` runs against the warehouse under the calling
# user's identity (OBO), so UC table grants apply per read and every query is
# audited. Swap for a `genie` space to ground on a curated semantic model
# instead of letting the agent author SQL.
tools:
  - type: sql
    warehouse_id: $WAREHOUSE_ID
    name: query_finance_data
    description: >
      Run SQL against the governed billing and general-ledger tables and return
      the rows. Auth is per-call user identity — the query sees only what the
      calling user has UC grants on.
```
