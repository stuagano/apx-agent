---
type: Unity Catalog Function
title: open_pprs
description: 'Open PPRs for a Mirion customer (source: SharePoint). Scalar function;
  returns rows as a JSON array.'
resource: serverless_stable_hvhhmh_catalog.mirion_precall.open_pprs
timestamp: '2026-08-13T21:00:00+00:00'
---

# Overview
Open PPRs (Product Problem Reports) for one customer from SharePoint — quality issues on Mirion product, analogous to a CAPA (Corrective and Preventive Action). Severity and status matter most: a Critical, Blocked PPR on a radiation-detection instrument is the single most important thing to surface before a call with a regulated customer.

# Parameters
- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.

# Returns
A JSON array (STRING) of matching rows; `[]` when the customer has none.

# Examples
### Any open quality issues for this customer?
```sql
SELECT serverless_stable_hvhhmh_catalog.mirion_precall.open_pprs('<company>')
```
- @company (STRING): the customer name

### Are there critical product problems to flag?
```sql
SELECT serverless_stable_hvhhmh_catalog.mirion_precall.open_pprs('<company>')
```
- @company (STRING): the customer name

# Synonyms
PPR, PPRs, product problem report, quality issue, CAPA, corrective action, defect
