---
type: Unity Catalog View
title: vw_rmas
description: 'Open RMAs for a Mirion customer (source: ServiceMax). Backs the `open_rmas`
  function.'
resource: serverless_stable_hvhhmh_catalog.mirion_precall.vw_rmas
timestamp: '2026-08-13T21:00:00+00:00'
---

# Overview
Open RMAs (Return Merchandise Authorizations) for one customer from ServiceMax — returned or failed instruments and their repair/replacement status. Open or Blocked RMAs are active service pain the rep must be ready to speak to; a returned radiation instrument is high-stakes for a regulated customer.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `company` | string | Customer / account name. Join key across every section. |
| `rma_id` | string | ServiceMax RMA number. |
| `description` | string | The affected unit / instrument being returned or repaired. |
| `status` | string | Open, In Progress, Blocked, or Closed. |
| `date` | string | RMA open date (ISO). |

# Joins
Every view in this schema joins to the others on `company`; one company's 7 sections together form its pre-call brief.

# Examples
### Any open returns or repairs for this customer?
```sql
SELECT * FROM serverless_stable_hvhhmh_catalog.mirion_precall.vw_rmas WHERE company = '<company>'
```
- @company (STRING): the customer name

Prefer the governed function `serverless_stable_hvhhmh_catalog.mirion_precall.open_rmas('<company>')`, which wraps this view.
