---
type: Unity Catalog Table
title: sales_franchises
description: Franchise (location) dimension — the bakery outlets that make sales.
resource: samples.bakehouse.sales_franchises
timestamp: '2026-08-14T20:00:00+00:00'
---

# Overview
The franchise dimension: one row per bakery location, with its geography, size, and the supplier that serves it. Join from `sales_transactions.franchiseID` to rank locations by revenue, or bridge to `sales_suppliers` via `supplierID`.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `franchiseID` | string | Primary key; join target for `sales_transactions.franchiseID`. |
| `name` | string | Franchise name. |
| `city` | string | Franchise city. |
| `district` | string | District / neighborhood. |
| `country` | string | Country. |
| `size` | string | Location size class (e.g. small/medium/large). |
| `supplierID` | string | Serving supplier — join to `sales_suppliers.supplierID`. |

# Joins
`franchiseID` ← `sales_transactions.franchiseID` and `← media_customer_reviews.franchiseID`; `supplierID` → `sales_suppliers`.

# Examples
### Top franchises by revenue
```sql
SELECT f.name, f.city, SUM(t.totalPrice) AS revenue
FROM samples.bakehouse.sales_transactions t
JOIN samples.bakehouse.sales_franchises f ON t.franchiseID = f.franchiseID
GROUP BY f.name, f.city ORDER BY revenue DESC LIMIT 10
```
