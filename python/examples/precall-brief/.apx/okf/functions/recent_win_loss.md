---
type: Unity Catalog Function
title: recent_win_loss
description: 'Recent Win / Loss for a customer (source: CRM). Scalar function; returns
  rows as a JSON array.'
resource: main.precall.recent_win_loss
timestamp: '2026-08-14T19:45:08.067955+00:00'
---

# Overview
Recently won and lost deals for one customer from your CRM. Recent wins show relationship momentum and which product lines are landing; recent losses are a competitive flag the rep should understand before the call.

# Parameters
- @company (STRING): the customer / account name to filter to, matched exactly against the `company` column.

# Returns
A JSON array (STRING) of matching rows; `[]` when the customer has none.

# Examples
### What has this customer bought recently?
```sql
SELECT main.precall.recent_win_loss('<company>')
```
- @company (STRING): the customer name

### Have we lost any deals here?
```sql
SELECT main.precall.recent_win_loss('<company>')
```
- @company (STRING): the customer name

# Synonyms
wins, losses, win loss, closed deals, recent deals, competitive losses
