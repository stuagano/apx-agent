---
type: Unity Catalog View
title: vw_rmas
description: 'Open RMAs for a customer (source: Field Service System). Backs the `open_rmas`
  function.'
resource: main.precall.vw_rmas
timestamp: '2026-08-14T19:45:08.067955+00:00'
---

# Overview
Open RMAs (Return Merchandise Authorizations) for one customer from your field-service system — returned or failed products and their repair/replacement status. Open or Blocked RMAs are active service issues the rep must be ready to speak to.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `company` | string | Customer / account name. Join key across every section. |
| `rma_id` | string | RMA number. |
| `description` | string | The affected unit / product being returned or repaired. |
| `status` | string | Open, In Progress, Blocked, or Closed. |
| `date` | string | RMA open date (ISO). |

# Joins
Every view in this schema joins to the others on `company`; one company's 7 sections together form its pre-call brief.

# Examples
### Any open returns or repairs for this customer?
```sql
SELECT * FROM main.precall.vw_rmas WHERE company = '<company>'
```
- @company (STRING): the customer name

Prefer the governed function `main.precall.open_rmas('<company>')`, which wraps this view.
