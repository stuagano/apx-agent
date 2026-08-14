---
type: Unity Catalog Table
title: media_customer_reviews
description: Free-text customer reviews — the source for sentiment and theme questions.
resource: samples.bakehouse.media_customer_reviews
timestamp: '2026-08-14T20:00:00+00:00'
---

# Overview
Free-text customer feedback, one row per review, tied to a franchise. This is the `reviews_agent`'s table — the customer's words live in `review`. Filter with `WHERE review LIKE '%...%'`, read the rows, and summarize sentiment/themes (quote briefly). For production-grade semantic retrieval, swap to a Vector Search index over `review` (see the example README "Upgrade").

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `review` | string | The customer's free-text review — the primary content to read and summarize. |
| `franchiseID` | string | Which location the review is about — join to `sales_franchises.franchiseID`. |
| `review_date` | date | When the review was written. |
| `new_id` | string | Row identifier. |

# Joins
`franchiseID` → `sales_franchises` (attribute review themes to a location).

# Examples
### What are customers saying about a product or location?
```sql
SELECT review, review_date, franchiseID
FROM samples.bakehouse.media_customer_reviews
WHERE review LIKE '%ginger%'
ORDER BY review_date DESC LIMIT 50
```
