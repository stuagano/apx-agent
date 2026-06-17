# Cost tracking

```python
from apx_agent import cost_for_agent
from databricks.sdk import WorkspaceClient

breakdown = cost_for_agent(
    agent_name="customer_triage",
    ws=WorkspaceClient(),
    lookback_hours=24,
    warehouse_id="wh-prod",
)
print(f"Total DBUs: {breakdown.total_dbus}")
print(f"Total USD:  ${breakdown.total_usd}")
for row in breakdown.rows:
    print(f"  {row['sku_name']}: {row['dbus']} DBUs → ${row['usd']}")
```

Queries `system.billing.usage` joined to `system.billing.list_prices` (best-effort) scoped to the agent's serving endpoint. `total_usd` is `None` when pricing isn't joinable for every row — partial coverage would understate the bill, so it's better to surface the gap than print a misleading total.

Same surface from the CLI:

```bash
apx-agent agents cost --agent customer_triage --hours 24
apx-agent agents cost --endpoint my-endpoint --hours 168 --format json
```

Requires the `system.billing` share to be enabled in the workspace.
