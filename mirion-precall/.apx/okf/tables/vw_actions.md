---
type: Unity Catalog View
title: vw_actions
description: 'Overdue Actions for a Mirion customer (source: Salesforce). Backs the
  `overdue_actions` function.'
resource: serverless_stable_hvhhmh_catalog.mirion_precall.vw_actions
timestamp: '2026-08-13T21:00:00+00:00'
---

# Overview
Committed follow-up actions for one customer from Salesforce — quotes to send, calls to make — with due date and status. Anything past its due date is a dropped ball the rep should close out on the call so nothing the account team promised slips.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `company` | string | Customer / account name. Join key across every section. |
| `action` | string | The committed follow-up. |
| `due_date` | string | When it was due (ISO). Past-due = overdue. |
| `status` | string | Open, In Progress, Blocked, or Closed. |

# Joins
Every view in this schema joins to the others on `company`; one company's 7 sections together form its pre-call brief.

# Examples
### What follow-ups are overdue for this customer?
```sql
SELECT * FROM serverless_stable_hvhhmh_catalog.mirion_precall.vw_actions WHERE company = '<company>'
```
- @company (STRING): the customer name

Prefer the governed function `serverless_stable_hvhhmh_catalog.mirion_precall.overdue_actions('<company>')`, which wraps this view.
