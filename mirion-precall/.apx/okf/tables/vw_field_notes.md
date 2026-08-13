---
type: Unity Catalog View
title: vw_field_notes
description: 'Field Notes for a Mirion customer (source: Salesforce). Backs the `field_notes`
  function.'
resource: serverless_stable_hvhhmh_catalog.mirion_precall.vw_field_notes
timestamp: '2026-08-13T21:00:00+00:00'
---

# Overview
Free-text field and supply-chain handoff notes for one customer from Salesforce — the human context: site visits, expressed product interest, relationship and logistics notes from the account team. Read these for signal the structured tables miss, including upsell interest not yet in the pipeline.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `company` | string | Customer / account name. Join key across every section. |
| `note` | string | Free-text note from a site visit or supply-chain handoff. |
| `author` | string | Who wrote the note. |
| `date` | string | Note date (ISO). |

# Joins
Every view in this schema joins to the others on `company`; one company's 7 sections together form its pre-call brief.

# Examples
### What are the latest field notes on this customer?
```sql
SELECT * FROM serverless_stable_hvhhmh_catalog.mirion_precall.vw_field_notes WHERE company = '<company>'
```
- @company (STRING): the customer name

Prefer the governed function `serverless_stable_hvhhmh_catalog.mirion_precall.field_notes('<company>')`, which wraps this view.
