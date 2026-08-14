---
type: Unity Catalog Function
title: open_rmas
description: 'Open RMAs for a customer (source: Field Service System). Scalar function;
  returns rows as a JSON array.'
resource: main.precall.open_rmas
timestamp: '2026-08-14T19:45:08.067955+00:00'
---

# Overview
Open RMAs (Return Merchandise Authorizations) for one customer from your field-service system — returned or failed products and their repair/replacement status. Open or Blocked RMAs are active service issues the rep must be ready to speak to.

# Parameters
- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.

# Returns
A JSON array (STRING) of matching rows; `[]` when the customer has none.

# Examples
### Any open returns or repairs for this customer?
```sql
SELECT main.precall.open_rmas('<company>')
```
- @company (STRING): the customer name

### What service issues are outstanding?
```sql
SELECT main.precall.open_rmas('<company>')
```
- @company (STRING): the customer name

# Synonyms
RMA, RMAs, returns, return authorization, repairs, service returns, failed units
