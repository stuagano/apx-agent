---
type: Unity Catalog Function
title: open_pprs
description: 'Open PPRs for a customer (source: Document Store). Scalar function;
  returns rows as a JSON array.'
resource: main.precall.open_pprs
timestamp: '2026-08-14T19:45:08.067955+00:00'
---

# Overview
Open PPRs (Product Problem Reports) for one customer from your document store — quality issues on your products, analogous to a CAPA (Corrective and Preventive Action). Severity and status matter most: a Critical, Blocked PPR is the single most important thing to surface before a call.

# Parameters
- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.

# Returns
A JSON array (STRING) of matching rows; `[]` when the customer has none.

# Examples
### Any open quality issues for this customer?
```sql
SELECT main.precall.open_pprs('<company>')
```
- @company (STRING): the customer name

### Are there critical product problems to flag?
```sql
SELECT main.precall.open_pprs('<company>')
```
- @company (STRING): the customer name

# Synonyms
PPR, PPRs, product problem report, quality issue, CAPA, corrective action, defect
