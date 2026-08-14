---
type: Unity Catalog Function
title: overdue_actions
description: 'Overdue Actions for a customer (source: CRM). Scalar function; returns
  rows as a JSON array.'
resource: main.precall.overdue_actions
timestamp: '2026-08-14T19:45:08.067955+00:00'
---

# Overview
Committed follow-up actions for one customer from your CRM — quotes to send, calls to make — with due date and status. Anything past its due date is a dropped ball the rep should close out on the call so nothing the account team promised slips.

# Parameters
- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.

# Returns
A JSON array (STRING) of matching rows; `[]` when the customer has none.

# Examples
### What follow-ups are overdue for this customer?
```sql
SELECT main.precall.overdue_actions('<company>')
```
- @company (STRING): the customer name

### What did we commit to this customer?
```sql
SELECT main.precall.overdue_actions('<company>')
```
- @company (STRING): the customer name

# Synonyms
actions, overdue actions, follow-ups, tasks, next steps, commitments, to-do
