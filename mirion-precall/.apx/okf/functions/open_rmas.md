---
type: Unity Catalog Function
title: open_rmas
description: 'Open RMAs for a Mirion customer (source: ServiceMax). Scalar function;
  returns rows as a JSON array.'
resource: serverless_stable_hvhhmh_catalog.mirion_precall.open_rmas
timestamp: '2026-08-13T21:00:00+00:00'
---

# Overview
Open RMAs (Return Merchandise Authorizations) for one customer from ServiceMax — returned or failed instruments and their repair/replacement status. Open or Blocked RMAs are active service pain the rep must be ready to speak to; a returned radiation instrument is high-stakes for a regulated customer.

# Parameters
- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.

# Returns
A JSON array (STRING) of matching rows; `[]` when the customer has none.

# Examples
### Any open returns or repairs for this customer?
```sql
SELECT serverless_stable_hvhhmh_catalog.mirion_precall.open_rmas('<company>')
```
- @company (STRING): the customer name

### What service issues are outstanding?
```sql
SELECT serverless_stable_hvhhmh_catalog.mirion_precall.open_rmas('<company>')
```
- @company (STRING): the customer name

# Synonyms
RMA, RMAs, returns, return authorization, repairs, service returns, failed units
