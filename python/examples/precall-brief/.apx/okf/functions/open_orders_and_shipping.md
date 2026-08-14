---
type: Unity Catalog Function
title: open_orders_and_shipping
description: 'Open Orders & Shipping for a customer (source: ERP). Scalar function;
  returns rows as a JSON array.'
resource: main.precall.open_orders_and_shipping
timestamp: '2026-08-14T19:45:08.067955+00:00'
---

# Overview
In-flight sales orders and shipment status for one customer from your ERP. The pre-call signal here is fulfillment risk: what products the customer is waiting on and whether any shipment is Blocked or slipping its expected ship date.

# Parameters
- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.

# Returns
A JSON array (STRING) of matching rows; `[]` when the customer has none.

# Examples
### What does this customer have on order?
```sql
SELECT main.precall.open_orders_and_shipping('<company>')
```
- @company (STRING): the customer name

### Are any shipments blocked or delayed?
```sql
SELECT main.precall.open_orders_and_shipping('<company>')
```
- @company (STRING): the customer name

# Synonyms
orders, open orders, shipments, shipping status, backlog, fulfillment
