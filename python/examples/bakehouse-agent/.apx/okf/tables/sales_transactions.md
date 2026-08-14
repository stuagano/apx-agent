---
type: Unity Catalog Table
title: sales_transactions
description: One row per bakery sale — the fact table for revenue, volume, and product-mix questions.
resource: samples.bakehouse.sales_transactions
timestamp: '2026-08-14T20:00:00+00:00'
---

# Overview
The transactions fact table: one row per item sold at a franchise. This is where every sales, revenue, and volume answer starts. Revenue is `SUM(totalPrice)` (not `unitPrice`, which is per-unit). Slice by `product`, `paymentMethod`, or `dateTime`, and join out to customers/franchises/suppliers on the `*ID` keys.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `transactionID` | string | Unique sale id (grain of the table). |
| `customerID` | string | Buyer — join to `sales_customers.customerID`. |
| `franchiseID` | string | Selling location — join to `sales_franchises.franchiseID`. |
| `dateTime` | timestamp | When the sale happened; truncate for daily/monthly trends. |
| `product` | string | Item sold (e.g. "Golden Gate Ginger", "Outback Oatmeal"). |
| `quantity` | bigint | Units in this line. |
| `unitPrice` | double | Price per unit. Do NOT sum this for revenue. |
| `totalPrice` | double | Line revenue (`quantity * unitPrice`). Revenue = `SUM(totalPrice)`. |
| `paymentMethod` | string | How the customer paid (card, cash, …). |

# Joins
`customerID` → `sales_customers`, `franchiseID` → `sales_franchises` (→ `supplierID` → `sales_suppliers`). The fact table anchors all sales joins.

# Examples
### What was total revenue by product?
```sql
SELECT product, SUM(totalPrice) AS revenue
FROM samples.bakehouse.sales_transactions
GROUP BY product ORDER BY revenue DESC
```
### Monthly revenue trend
```sql
SELECT date_trunc('month', dateTime) AS month, SUM(totalPrice) AS revenue
FROM samples.bakehouse.sales_transactions
GROUP BY 1 ORDER BY 1
```
