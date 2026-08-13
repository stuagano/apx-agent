---
type: Unity Catalog Function
title: open_orders_and_shipping
description: 'Open Orders & Shipping for a Mirion customer (source: SAP). Scalar function;
  returns rows as a JSON array.'
resource: serverless_stable_hvhhmh_catalog.mirion_precall.open_orders_and_shipping
timestamp: '2026-08-13T21:00:00+00:00'
---

# Overview
In-flight sales orders and shipment status for one customer, sourced from SAP. The pre-call signal here is fulfillment risk: what hardware the customer is waiting on and whether any shipment is Blocked or slipping its expected ship date. A blocked detector or dosimeter order is a conversation the rep must lead with.

# Parameters
- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.

# Returns
A JSON array (STRING) of matching rows; `[]` when the customer has none.

# Examples
### What does this customer have on order?
```sql
SELECT serverless_stable_hvhhmh_catalog.mirion_precall.open_orders_and_shipping('<company>')
```
- @company (STRING): the customer name

### Are any shipments blocked or delayed?
```sql
SELECT serverless_stable_hvhhmh_catalog.mirion_precall.open_orders_and_shipping('<company>')
```
- @company (STRING): the customer name

# Synonyms
orders, open orders, shipments, shipping status, backlog, SAP orders, fulfillment
