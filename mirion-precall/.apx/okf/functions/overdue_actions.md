---
type: Unity Catalog Function
title: overdue_actions
description: 'Overdue Actions for a Mirion customer (source: Salesforce). Scalar function;
  returns rows as a JSON array.'
resource: serverless_stable_hvhhmh_catalog.mirion_precall.overdue_actions
timestamp: '2026-08-13T21:00:00+00:00'
---

# Overview
Committed follow-up actions for one customer from Salesforce — quotes to send, calls to make — with due date and status. Anything past its due date is a dropped ball the rep should close out on the call so nothing the account team promised slips.

# Parameters
- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.

# Returns
A JSON array (STRING) of matching rows; `[]` when the customer has none.

# Examples
### What follow-ups are overdue for this customer?
```sql
SELECT serverless_stable_hvhhmh_catalog.mirion_precall.overdue_actions('<company>')
```
- @company (STRING): the customer name

### What did we commit to this customer?
```sql
SELECT serverless_stable_hvhhmh_catalog.mirion_precall.overdue_actions('<company>')
```
- @company (STRING): the customer name

# Synonyms
actions, overdue actions, follow-ups, tasks, next steps, commitments, to-do
