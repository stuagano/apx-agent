---
type: Unity Catalog View
title: vw_pprs
description: 'Open PPRs for a Mirion customer (source: SharePoint). Backs the `open_pprs`
  function.'
resource: serverless_stable_hvhhmh_catalog.mirion_precall.vw_pprs
timestamp: '2026-08-13T21:00:00+00:00'
---

# Overview
Open PPRs (Product Problem Reports) for one customer from SharePoint — quality issues on Mirion product, analogous to a CAPA (Corrective and Preventive Action). Severity and status matter most: a Critical, Blocked PPR on a radiation-detection instrument is the single most important thing to surface before a call with a regulated customer.

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
SELECT * FROM serverless_stable_hvhhmh_catalog.mirion_precall.vw_pprs WHERE company = '<company>'
```
- @company (STRING): the customer name

Prefer the governed function `serverless_stable_hvhhmh_catalog.mirion_precall.open_pprs('<company>')`, which wraps this view.
