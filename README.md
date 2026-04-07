# apx-agent

Standalone agent runtime for Databricks Apps — MCP, A2A, /invocations, tool routing.

## Quick start

```python
from apx_agent import Agent, Dependencies, create_app

def get_billing(customer_id: str, ws: Dependencies.Client) -> dict:
    """Get billing history."""
    ...

agent = Agent(tools=[get_billing])
app = create_app(agent)
```

```bash
uvicorn my_app:app --reload
```
