---
type: Unity Catalog Table
title: tickets
description: HubSpot support tickets synced into Unity Catalog — the source for complaint summaries.
resource: placeholder_catalog.placeholder_schema.tickets
timestamp: '2026-08-14T20:00:00+00:00'
---

# Overview
HubSpot support tickets synced into Unity Catalog (one row per ticket). This is the agent's only table: read `subject` + `content` to group complaints into recurring themes, and count tickets per month. A ticket's **month is defined by `hs_createdate`** (when it was opened), not when it closed. Catalog/schema are placeholders — set `APX_CATALOG` / `APX_SCHEMA` / `APX_TICKETS_TABLE` for your synced table.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `hs_object_id` | string | HubSpot ticket id (row grain). |
| `subject` | string | Ticket subject line — the short complaint headline; group these into themes. |
| `content` | string | Ticket body — the detailed complaint text. |
| `hs_createdate` | timestamp | When the ticket was created; defines the complaint's month. |
| `hs_pipeline_stage` | string | Current stage in the support pipeline (open, waiting, closed, …). |

# Examples
### How many complaints were filed in a given month, and about what?
```sql
SELECT hs_object_id, subject, content, hs_pipeline_stage
FROM placeholder_catalog.placeholder_schema.tickets
WHERE CAST(date_trunc('month', hs_createdate) AS DATE) = DATE '2026-06-01'
```
### Monthly ticket volume trend
```sql
SELECT date_trunc('month', hs_createdate) AS month, COUNT(*) AS tickets
FROM placeholder_catalog.placeholder_schema.tickets
GROUP BY 1 ORDER BY 1
```
