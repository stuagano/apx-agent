---
type: Unity Catalog Table
title: sales_suppliers
description: Supplier dimension — the ingredient suppliers behind each franchise.
resource: samples.bakehouse.sales_suppliers
timestamp: '2026-08-14T20:00:00+00:00'
---

# Overview
The supplier dimension: one row per ingredient supplier, with what they supply and whether they're approved. Reach it from `sales_franchises.supplierID`; useful for supply-chain and sourcing questions (e.g. revenue attributable to an approved supplier's franchises).

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `supplierID` | string | Primary key; join target for `sales_franchises.supplierID`. |
| `name` | string | Supplier name. |
| `ingredient` | string | Ingredient supplied (e.g. flour, sugar, chocolate). |
| `continent` | string | Supplier continent. |
| `city` | string | Supplier city. |
| `approved` | string | Whether the supplier is approved (governance flag). |

# Joins
`supplierID` ← `sales_franchises.supplierID` (one supplier → many franchises → transactions).

# Examples
### Revenue by supplier ingredient
```sql
SELECT s.ingredient, SUM(t.totalPrice) AS revenue
FROM samples.bakehouse.sales_transactions t
JOIN samples.bakehouse.sales_franchises f ON t.franchiseID = f.franchiseID
JOIN samples.bakehouse.sales_suppliers s ON f.supplierID = s.supplierID
GROUP BY s.ingredient ORDER BY revenue DESC
```
