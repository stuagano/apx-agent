---
type: Databricks Schema
title: placeholder_schema
description: 'HubSpot support tickets synced into Unity Catalog; one tickets table backing monthly complaint summaries. Catalog/schema are env-overridable (APX_CATALOG / APX_SCHEMA).'
resource: placeholder_catalog.placeholder_schema
catalog: placeholder_catalog
schema: placeholder_schema
timestamp: '2026-08-14T20:00:00+00:00'
---

# Tables
* [tickets](../tables/tickets.md)

# Glossary
### Complaint
A customer-reported problem captured as a HubSpot support ticket (`tickets`). This agent reads ticket `subject`/`content` to summarize complaints. Synonyms: issue, problem, ticket, case.

### Theme
A recurring category the agent groups tickets into (e.g. "billing confusion", "shipping delays"), each with a count and example subjects. Synonyms: category, topic, bucket, cluster.

### Ticket month
The month a complaint belongs to, defined by `hs_createdate` (creation), NOT close date. All monthly counts and comparisons key off this. Synonyms: reporting month, created month.

### Pipeline stage
The ticket's position in the support workflow (`hs_pipeline_stage`) — e.g. open, waiting on customer, closed. Synonyms: status, stage, state.
