# customer_triage — worked example

A customer-support triage agent that exercises the full apx-agent surface end-to-end.

```
                       triage (classifier)
                              │
                ┌─────────────┼─────────────┐
                │             │             │
        billing_specialist  technical_  account_
                            specialist  specialist
        ─────────────────  ──────────  ───────────
        get_recent_orders  docs_search ask_account_data
        format_address     (vector_    (genie)
        (SQL warehouse)     search)
```

The `triage` LlmAgent calls `classify_intent` (a UC function) on the user's query, then transfers control to the right specialist via the auto-generated `transfer_to_*` tools. Each specialist exercises a different platform primitive — SQL, vector search, Genie — so the example walks through every governed-primitive shape.

## What this example demonstrates

| apx-agent feature | Where in this example |
|---|---|
| `@tool(uc="...")` | `classify_intent`, `format_address` — pure Python, sync to UC, governed |
| `Dependencies.Workspace` injection | `get_recent_orders` — user-scoped SQL via OBO |
| `vector_search_tool` | `technical_specialist` agent — docs retrieval |
| `genie_tool` | `account_specialist` agent — natural-language account data |
| `HandoffAgent` | Top-level — routes to specialists mid-conversation |
| Resource auto-declaration | `apx deploy` walks the tree, declares everything to MLflow |
| Eval (`evalset.jsonl`) | 8 queries spanning the four intent buckets |

## Local development

```bash
# From this directory:
uv pip install -e ../..    # install apx-agent in editable mode
apx info                   # inspect what's declared (no Databricks calls)
apx run                    # uvicorn against app.py:app
```

`apx info` shows the agent's declared tools, sub-agents, and Mosaic AI resources at a glance — useful before deploying.

## Publish + deploy

```bash
# 1. Publish UC-syncable tools (classify_intent, format_address)
apx publish-tools --module agent:agent --dry-run    # preview
apx publish-tools --module agent:agent              # actually create + grant

# 2. Log the ChatAgent to MLflow + deploy to Model Serving
export ACCOUNT_GENIE_SPACE_ID=abc-123  # used by ask_account_data
apx deploy --module agent:agent \
           --model databricks-claude-sonnet-4-6 \
           --name main.agents.customer_triage

# 3. Register as a Supervisor sub-agent (optional)
apx publish --endpoint customer_triage --supervisor sa-12345 \
            --description "Routes customer support queries to specialists."

# 4. Generate Claude Desktop / Cursor MCP config (optional)
apx mcp-config --module agent:agent --host "$DATABRICKS_HOST" --name triage
```

## Evaluate

```bash
apx eval evalset.jsonl --module agent:agent --model databricks-claude-sonnet-4-6
```

The evalset checks routing accuracy — each query has an `expected_intent` field. With Mosaic AI Agent Evaluation's default scorers, you'll get correctness and relevance metrics; add a custom scorer to gate on the transfer tool that actually got called if you want strict routing-accuracy enforcement.

## Sessions

To turn this into a multi-turn conversational agent, wire a `SessionStore` at deploy time:

```python
from apx_agent import compile_to_chat_agent, DeltaSessionStore
from databricks.sdk import WorkspaceClient

ws = WorkspaceClient()
session_store = DeltaSessionStore(
    table_path="main.agents.customer_triage_sessions",
    ws=ws,
    warehouse_id="wh-prod",
)
chat = compile_to_chat_agent(agent, model="...", session_store=session_store)
```

Then `predict(messages, custom_inputs={"session_id": "user:alice:thread-7"})` carries history across turns automatically.
