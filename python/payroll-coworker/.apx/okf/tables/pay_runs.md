---
type: Unity Catalog Table
title: pay_runs
description: pay_runs table.
resource: serverless_stable_qh44kx_catalog.payroll_demo.pay_runs
timestamp: '2026-06-17T03:06:48.231159+00:00'
---

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `run_id` | string |  |
| `period_end` | date |  |
| `employee_id` | string |  |
| `gross_pay` | decimal(6,2) |  |
| `overtime_hours` | int |  |
| `deductions` | decimal(6,2) |  |
| `net_pay` | decimal(6,2) |  |

# Overview
One row per employee per pay period; the core payroll fact table.

# Joins
Join to [`employees`](/tables/employees.md) on `employee_id` to attribute pay to a worker.

# Examples
```sql
SELECT employee_id, gross_pay FROM pay_runs WHERE period_end = '2026-05-31'
```
