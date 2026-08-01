---
type: Unity Catalog Table
title: customer_support_logs
description: customer_support_logs table.
resource: samples.wanderbricks.customer_support_logs
timestamp: '2026-08-01T14:56:19.006980+00:00'
---

# Schema
| Column | Type | Description |
| --- | --- | --- |
| `created_at` | string |  |
| `messages` | array<struct<message:string,sender:string,sentiment:string,timestamp:string>> |  |
| `support_agent_id` | string |  |
| `ticket_id` | string |  |
| `user_id` | bigint |  |
