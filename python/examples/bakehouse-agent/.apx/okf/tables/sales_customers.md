---
type: Unity Catalog Table
title: sales_customers
description: Customer dimension — who bought, and where they are.
resource: samples.bakehouse.sales_customers
timestamp: '2026-08-14T20:00:00+00:00'
---

# Overview
The customer dimension: one row per buyer, with name and geography. Join from `sales_transactions.customerID` to segment revenue by `country` / `continent` / `state`, or to count distinct customers.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `customerID` | string | Primary key; join target for `sales_transactions.customerID`. |
| `first_name` | string | Customer given name. |
| `last_name` | string | Customer family name. |
| `city` | string | Customer city. |
| `state` | string | State / region. |
| `country` | string | Country. |
| `continent` | string | Continent — useful for coarse geo rollups. |
| `gender` | string | Customer-reported gender. |

# Joins
`customerID` ← `sales_transactions.customerID` (one customer → many transactions).

# Examples
### Revenue by customer country
```sql
SELECT c.country, SUM(t.totalPrice) AS revenue
FROM samples.bakehouse.sales_transactions t
JOIN samples.bakehouse.sales_customers c ON t.customerID = c.customerID
GROUP BY c.country ORDER BY revenue DESC
```
