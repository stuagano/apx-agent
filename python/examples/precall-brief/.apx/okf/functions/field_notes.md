---
type: Unity Catalog Function
title: field_notes
description: 'Field Notes for a customer (source: CRM). Scalar function; returns rows
  as a JSON array.'
resource: main.precall.field_notes
timestamp: '2026-08-14T19:45:08.067955+00:00'
---

# Overview
Free-text field and supply-chain handoff notes for one customer from your CRM — the human context: site visits, expressed product interest, relationship and logistics notes from the account team. Read these for signal the structured tables miss, including upsell interest not yet in the pipeline.

# Parameters
- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.

# Returns
A JSON array (STRING) of matching rows; `[]` when the customer has none.

# Examples
### What are the latest field notes on this customer?
```sql
SELECT main.precall.field_notes('<company>')
```
- @company (STRING): the customer name

### What interest has the customer expressed?
```sql
SELECT main.precall.field_notes('<company>')
```
- @company (STRING): the customer name

# Synonyms
field notes, notes, site visit notes, handoff notes, account notes, supply chain notes
