---
type: Unity Catalog View
title: vw_winloss
description: 'Recent Win / Loss for a Mirion customer (source: Salesforce). Backs
  the `recent_win_loss` function.'
resource: serverless_stable_hvhhmh_catalog.mirion_precall.vw_winloss
timestamp: '2026-08-13T21:00:00+00:00'
---

# Overview
Recently won and lost deals for one customer from Salesforce. Recent wins show relationship momentum and which product lines are landing; recent losses are a competitive flag the rep should understand before the call.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `company` | string | Customer / account name. Join key across every section. |
| `outcome` | string | Won or Lost. |
| `product` | string | Product line involved in the deal. |
| `date` | string | Date the deal closed (ISO). |

# Joins
Every view in this schema joins to the others on `company`; one company's 7 sections together form its pre-call brief.

# Examples
### What has this customer bought recently?
```sql
SELECT * FROM serverless_stable_hvhhmh_catalog.mirion_precall.vw_winloss WHERE company = '<company>'
```
- @company (STRING): the customer name

Prefer the governed function `serverless_stable_hvhhmh_catalog.mirion_precall.recent_win_loss('<company>')`, which wraps this view.
