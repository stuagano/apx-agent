---
type: Unity Catalog Function
title: open_opportunities
description: 'Open Opportunities for a Mirion customer (source: Salesforce). Scalar
  function; returns rows as a JSON array.'
resource: serverless_stable_hvhhmh_catalog.mirion_precall.open_opportunities
timestamp: '2026-08-13T21:00:00+00:00'
---

# Overview
Active sales pipeline for one customer from Salesforce — the open deals, their stage, dollar value, and close date. Use it to walk into the call knowing deal size and where each opportunity sits, especially anything in Proposal or Negotiation with a near-term close.

# Parameters
- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.

# Returns
A JSON array (STRING) of matching rows; `[]` when the customer has none.

# Examples
### What's in the pipeline for this customer?
```sql
SELECT serverless_stable_hvhhmh_catalog.mirion_precall.open_opportunities('<company>')
```
- @company (STRING): the customer name

### Which deals are close to closing?
```sql
SELECT serverless_stable_hvhhmh_catalog.mirion_precall.open_opportunities('<company>')
```
- @company (STRING): the customer name

# Synonyms
opportunities, pipeline, open deals, deals, sales pipeline, opps
