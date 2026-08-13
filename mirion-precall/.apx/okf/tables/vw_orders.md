---
type: Unity Catalog View
title: vw_orders
description: 'Open Orders & Shipping for a Mirion customer (source: SAP). Backs the
  `open_orders_and_shipping` function.'
resource: serverless_stable_hvhhmh_catalog.mirion_precall.vw_orders
timestamp: '2026-08-13T21:00:00+00:00'
---

# Overview
In-flight sales orders and shipment status for one customer, sourced from SAP. The pre-call signal here is fulfillment risk: what hardware the customer is waiting on and whether any shipment is Blocked or slipping its expected ship date. A blocked detector or dosimeter order is a conversation the rep must lead with.

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `company` | string | Customer / account name. Join key across every section. |
| `order_id` | string | SAP sales-order number (e.g. ORDER-7988). |
| `description` | string | Ordered product — detector, dosimeter, survey meter, spectrometer, probe, or camera. |
| `qty` | bigint | Units ordered. |
| `expected_ship` | string | Committed ship date (ISO). Compare to today to spot slips. |
| `status` | string | Fulfillment status: Open, In Progress, Blocked, or Closed. 'Blocked' is the escalation signal. |

# Joins
Every view in this schema joins to the others on `company`; one company's 7 sections together form its pre-call brief.

# Examples
### What does this customer have on order?
```sql
SELECT * FROM serverless_stable_hvhhmh_catalog.mirion_precall.vw_orders WHERE company = '<company>'
```
- @company (STRING): the customer name

Prefer the governed function `serverless_stable_hvhhmh_catalog.mirion_precall.open_orders_and_shipping('<company>')`, which wraps this view.
