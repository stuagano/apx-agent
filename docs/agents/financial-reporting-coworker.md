# Financial Reporting Coworker — Billing × General Ledger

A deep dive on one entry from the [coworker use-case catalog](coworker-use-cases.md).
Origin: a real customer onboarding thread about internal reporting.

- **Runnable spec:** [`coworkers/financial-reporting.yaml`](../coworkers/financial-reporting.yaml)
- **Runnable synthetic proof:** [`coworkers/examples/financial-reporting-synthetic.yaml`](../coworkers/examples/financial-reporting-synthetic.yaml)
- **Join key:** invoice ID
- **Billing system:** what was invoiced to customers
- **General ledger:** what revenue was posted to the books

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

## Phasing

How this actually lands with a customer:

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

For the fuller argument on why an agent belongs here at all — and where a plain
scheduled job plus a dashboard is the right tool instead — see
[Why an agent, and not just a job and a dashboard](coworker-use-cases.md#why-an-agent-and-not-just-a-job-and-a-dashboard).

## Proven end-to-end

On 2026-07-21 the coworker was run against synthetic UC tables via
`apx-agent agents run`. It authored SQL against the governed tables under the
caller's identity and correctly separated three cases — an unposted invoice, a
short-posted amount (flagged as a true error), and a period-cut timing difference
(flagged as *not* an error). Reproduce with the synthetic proof spec linked above.
