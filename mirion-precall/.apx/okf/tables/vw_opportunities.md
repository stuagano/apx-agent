---
type: Unity Catalog View
title: vw_opportunities
description: 'Open Opportunities for a Mirion customer (source: Salesforce). Backs
  the `open_opportunities` function.'
resource: serverless_stable_hvhhmh_catalog.mirion_precall.vw_opportunities
timestamp: '2026-08-13T21:00:00+00:00'
---

# Overview
Active sales pipeline for one customer from Salesforce — the open deals, their stage, dollar value, and close date. Use it to walk into the call knowing deal size and where each opportunity sits, especially anything in Proposal or Negotiation with a near-term close.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `company` | string | Customer / account name. Join key across every section. |
| `opportunity` | string | Deal name, usually a product-line fleet upgrade. |
| `stage` | string | Sales stage: Discovery, Qualification, Proposal, Negotiation, Closed Won. |
| `value` | bigint | Deal value in USD. |
| `close_date` | string | Expected close date (ISO). |

# Joins
Every view in this schema joins to the others on `company`; one company's 7 sections together form its pre-call brief.

# Examples
### What's in the pipeline for this customer?
```sql
SELECT * FROM serverless_stable_hvhhmh_catalog.mirion_precall.vw_opportunities WHERE company = '<company>'
```
- @company (STRING): the customer name

Prefer the governed function `serverless_stable_hvhhmh_catalog.mirion_precall.open_opportunities('<company>')`, which wraps this view.
