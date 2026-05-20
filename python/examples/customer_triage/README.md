# customer_triage — worked example

A customer-support triage agent that exercises the full apx-agent surface end-to-end. Deploys to **either** Model Serving **or** Databricks Apps via `apx deploy --target {model-serving,apps}`.

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
| `InMemoryMemoryStore` + `make_memory_tools` | `account_specialist` — principal-keyed `recall` / `remember` / `forget` for prefs that outlive the session |
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

This example ships two deploy paths — pick by workload. The full tradeoff write-up is in [`docs/apps-vs-model-serving.md`](../../../docs/apps-vs-model-serving.md).

### Option A: Model Serving (`--target model-serving`, default)

For production endpoints recognized by AI Playground, Review App, Supervisor Agent. Container build pipeline.

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

### Option B: Databricks Apps (`--target apps`)

For fast iteration. Code-push deploy via `databricks bundle deploy + bundle run`; no container build. Files in `agent_server/`, `databricks.yml`, `pyproject.toml`.

```bash
cd python/examples/customer_triage
uv sync
uv run quickstart                      # creates MLflow experiment, writes .env
apx deploy --target apps               # bundle deploy + bundle run

# After deploy, query the live app:
curl -X POST https://customer-triage-<workspace-id>.<region>.databricksapps.com/invocations \
  -H "Authorization: Bearer $(databricks auth token --profile <p> | jq -r .access_token)" \
  -H "Content-Type: application/json" \
  -d '{"input":[{"role":"user","content":"why is my bill so high?"}]}'
```

`APX_SMOKE_MODE=1` (set in `databricks.yml`'s `env` block by default) swaps the UC / Genie / Vector Search tool references for inline stubs so the Apps deploy works without pre-provisioning workspace resources. Remove the env var (or set it to anything else) to run against real resources.

Memory recall **works across the HandoffAgent boundary** — principal-keyed memory survives sub-agent transitions because the key is the user, not the session. Verified live: a query routed to `account_specialist` correctly invokes the `recall` tool and returns Alice's seeded preferences.

## Evaluate

```bash
apx eval evalset.jsonl --module agent:agent --model databricks-claude-sonnet-4-6
```

The evalset checks routing accuracy — each query has an `expected_intent` field. With Mosaic AI Agent Evaluation's default scorers, you'll get correctness and relevance metrics; add a custom scorer to gate on the transfer tool that actually got called if you want strict routing-accuracy enforcement.

## Memory

The `account_specialist` sub-agent is the one with memory wired in.

**Why account, not billing or technical?** Account work leans hardest on per-user preferences: preferred notification channel, language, recovery email, security-event history. Billing is transactional (UC governs the order data already). Technical leans on docs retrieval more than on remembered facts about the user. So `account_specialist` gets the memory store; the other two sub-agents stay clean.

**What's wired in:**

```python
# In agent.py
account_memory_store = InMemoryMemoryStore()  # see lakebase swap below
account_memory_tools = make_memory_tools(
    store=account_memory_store,
    default_principal_id="user:alice",
    namespace_default="profile",
)

account_agent = Agent(
    name="account_specialist",
    instructions="... call `recall` first ... call `remember` when the user shares a new preference ...",
    tools=[*account_memory_tools, genie_tool(...)],
)
```

`make_memory_tools` mints three `@tool`-decorated callables — `recall`, `remember`, `forget` — closed over the store. The LLM sees them as ordinary tools and decides when to invoke them per the instructions.

**The recall + remember pattern alongside HandoffAgent:**

Memory is keyed by `principal_id`, not by `session_id`. That's the load-bearing fact for handoff routing:

- A turn routed `triage -> billing` and back to `triage -> account` finds the same memories — they're scoped to *the user*, not to *the conversation*.
- A new conversation tomorrow under a brand-new `session_id` for the same user still sees yesterday's memories.
- Memory survives sub-agent restarts, container redeploys, and (with a durable store) full process restarts.

This is why memory and sessions are sibling primitives, not nested. Sessions hold short-lived conversational state. Memory holds long-lived facts about a principal.

**Swap to `LakebaseMemoryStore` for production:**

```python
from sqlalchemy import create_engine, event
from databricks.sdk import WorkspaceClient
from apx_agent import LakebaseMemoryStore

ws = WorkspaceClient()
engine = create_engine("postgresql+psycopg://app@<host>:5432/agentdb")

@event.listens_for(engine, "do_connect")
def add_oauth_token(_dialect, _record, _args, kwargs):
    cred = ws.database.generate_database_credential(
        instance_names=["my-lakebase-instance"], request_id="customer-triage",
    )
    kwargs["password"] = cred.token

def embed(texts):
    return ws.serving_endpoints.query(
        name="databricks-gte-large-en", inputs={"input": list(texts)}
    ).predictions

account_memory_store = LakebaseMemoryStore(
    engine=engine, embedding_fn=embed, embedding_dim=1024,
)
```

See [`docs/lakebase-recipe.md`](../../../docs/lakebase-recipe.md) for the full Lakebase provisioning + pgvector walkthrough, including the `principal_id` index and the ivfflat tuning knob.

**Resolving `principal_id` in production:**

The seeded example pins `default_principal_id="user:alice"` so local dev runs end-to-end without auth wiring. In a real deployment, swap that for the calling user's identity:

```python
def _resolve_principal() -> str | None:
    # Pull from the per-request OBO WorkspaceClient — wired in by the harness.
    ws = WorkspaceClient()
    me = ws.current_user.me()
    return f"user:{me.user_name}" if me.user_name else None

account_memory_tools = make_memory_tools(
    store=account_memory_store,
    principal_id_resolver=_resolve_principal,
    namespace_default="profile",
)
```

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
