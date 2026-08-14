---
type: Unity Catalog View
title: vw_orders
description: 'Open Orders & Shipping for a customer (source: ERP). Backs the `open_orders_and_shipping`
  function.'
resource: main.precall.vw_orders
timestamp: '2026-08-14T19:45:08.067955+00:00'
---

# Overview
In-flight sales orders and shipment status for one customer from your ERP. The pre-call signal here is fulfillment risk: what products the customer is waiting on and whether any shipment is Blocked or slipping its expected ship date.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `company` | string | Customer / account name. Join key across every section. |
| `order_id` | string | Order number (e.g. ORDER-7988). |
| `description` | string | Ordered product line. |
| `qty` | bigint | Units ordered. |
| `expected_ship` | string | Committed ship date (ISO). Compare to today to spot slips. |
| `status` | string | Fulfillment status: Open, In Progress, Blocked, or Closed. 'Blocked' is the escalation signal. |

# Joins
Every view in this schema joins to the others on `company`; one company's 7 sections together form its pre-call brief.

# Examples
### What does this customer have on order?
```sql
SELECT * FROM main.precall.vw_orders WHERE company = '<company>'
```
- @company (STRING): the customer name

Prefer the governed function `main.precall.open_orders_and_shipping('<company>')`, which wraps this view.
