---
type: Unity Catalog View
title: vw_pprs
description: 'Open PPRs for a customer (source: Document Store). Backs the `open_pprs`
  function.'
resource: main.precall.vw_pprs
timestamp: '2026-08-14T19:45:08.067955+00:00'
---

# Overview
Open PPRs (Product Problem Reports) for one customer from your document store — quality issues on your products, analogous to a CAPA (Corrective and Preventive Action). Severity and status matter most: a Critical, Blocked PPR is the single most important thing to surface before a call.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `company` | string | Customer / account name. Join key across every section. |
| `ppr_id` | string | Product Problem Report id. |
| `description` | string | The quality issue and affected unit. |
| `severity` | string | Low, Medium, High, or Critical. Critical is the escalation signal. |
| `status` | string | Open, In Progress, Blocked, or Closed. |

# Joins
Every view in this schema joins to the others on `company`; one company's 7 sections together form its pre-call brief.

# Examples
### Any open quality issues for this customer?
```sql
SELECT * FROM main.precall.vw_pprs WHERE company = '<company>'
```
- @company (STRING): the customer name

Prefer the governed function `main.precall.open_pprs('<company>')`, which wraps this view.
