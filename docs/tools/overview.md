# Tools

Tools give agents the ability to take action — query data, call APIs, run code, and delegate to other agents. The LLM decides which tools to call and with what arguments; the framework executes them and feeds results back into the model loop.

## What are tools

A tool is any Python function in the `tools=[...]` list passed to `Agent`. The function's type hints become the LLM-visible parameter schema; the docstring becomes the tool description. No registration step, no schema file — type your function and it's a tool.

```python
from apx_agent import Agent, tool

@tool
def get_order_status(order_id: str) -> str:
    """Return the current status of a customer order."""
    return db.query("SELECT status FROM orders WHERE id = ?", order_id)

agent = Agent(
    instructions="You are an order support assistant.",
    tools=[get_order_status],
)
```

Tools fall into three categories:

| Category | Examples |
|----------|---------|
| **Function tools** | `@tool`-decorated Python functions you write |
| **Built-in tools** | `sql_tool`, `genie_tool`, `vector_search_tool`, `uc_function_tool`, … |
| **Advanced tools** | `http_tool`, `openapi_tool`, `mcp_tool`, `mcp_toolkit`, `agent_tool` |

---

## Function tools — `@tool`

The `@tool` decorator is the primary way to define a tool. It's optional — plain functions work too — but the decorator enables name/description overrides and UC publishing.

```python
from apx_agent import tool

@tool
def classify_intent(query: str) -> str:
    """Classify a customer query as billing/technical/account/other."""
    return "billing" if "bill" in query.lower() else "other"
```

### Override name and description

Use the parameterized form when the function name or docstring isn't what you want the LLM to see:

```python
@tool(
    name="lookup_order",
    description="Look up the current status of a customer order by ID.",
)
def get_order_status(order_id: str) -> str:
    ...
```

### Injected context — `Dependencies.*`

Tools running inside a Databricks App receive injected parameters from FastAPI — workspace clients, SQL runners, the current user's identity. Declare them with `Dependencies.*` type aliases and they are excluded from the LLM's input schema.

```python
from apx_agent import tool, Dependencies

@tool
def get_jobs_for_table(table_full_name: str, ws: Dependencies.Workspace) -> list[dict]:
    """Find Databricks Jobs that write to a Unity Catalog table."""
    return ws.jobs.list(...)
```

Available injected types:

| Alias | What you get |
|-------|-------------|
| `Dependencies.Workspace` | `WorkspaceClient` authenticated as the **current user** (OBO token) |
| `Dependencies.Client` | `WorkspaceClient` using the app's service principal |
| `Dependencies.Sql` | SQL runner bound to the current user |
| `Dependencies.Principal` | Current user's username string, or `None` locally |
| `Dependencies.Progress` | Callable to emit a progress marker into the trace |
| `Dependencies.Request` | Raw FastAPI `Request` object |

### UC-syncable tools

Add `uc=` to publish the function to Unity Catalog at deploy time. This makes it callable from Genie spaces, Managed MCP, and other agents browsing the catalog — without redefinition.

```python
@tool(
    uc="main.tools.classify_intent",
    grant=["account users"],
)
def classify_intent(query: str) -> str:
    """Classify a customer query as billing/technical/account/other."""
    ...
```

`grant` sets `EXECUTE` on the UC function for the listed principals. It is only valid when `uc=` is set.

**Constraint:** UC-syncable tools cannot use `Dependencies.*` parameters. UC functions execute server-side under the function owner's identity, so a user-scoped `WorkspaceClient` is unavailable.

After defining UC-syncable tools, publish them with one call:

```python
from apx_agent import publish_tools_to_uc

publish_tools_to_uc(agent)   # registers + grants in UC, idempotent
```

### Declaring custom resources

When your tool accesses a specific Databricks asset, declare it so `log_agent` can include it in the Model Serving resource manifest:

```python
from apx_agent import ResourceSpec, attach_resources

@tool
def query_orders(question: str, ws: Dependencies.Workspace) -> str:
    """Query the orders table."""
    return ws.statement_execution.execute_statement(
        f"SELECT * FROM main.sales.orders WHERE ..."
    )

attach_resources(query_orders, [ResourceSpec("uc_table", "main.sales.orders")])
```

---

## Built-in tools — Databricks platform factories

These are pre-built tools for the Databricks platform. Each factory returns a ready-to-use tool (or list of tools) and automatically attaches the required resource declaration for `log_agent`.

| Factory | What it does | Resource declared |
|---------|--------------|-------------------|
| `uc_function_tool(name)` | Execute a registered UC function. Schema auto-derived from UC. | `DatabricksFunction` |
| `uc_function_toolkit(schema)` | All agent-facing functions in a UC schema as tools | `DatabricksFunction` (×N) |
| `genie_tool(space_id)` | Ask a natural-language question to a Genie space | `DatabricksGenieSpace` |
| `vector_search_tool(index_name)` | Query a Vector Search index — top-k results, optional column projection | `DatabricksVectorSearchIndex` |
| `sql_tool(warehouse_id=...)` | Run arbitrary SQL against a SQL warehouse | `DatabricksSQLWarehouse` |
| `foundation_model_tool(endpoint)` | Ask a Foundation Model endpoint — agent-to-model routing | `DatabricksServingEndpoint` |
| `lineage_tool()` | Get upstream/downstream lineage for a UC table | — |
| `schema_tool()` | Describe columns of a UC table | — |
| `catalog_tool(catalog, schema)` | List tables in a UC schema | — |

```python
from apx_agent import (
    Agent, genie_tool, vector_search_tool, sql_tool, foundation_model_tool,
)

agent = Agent(
    instructions="Answer questions using docs, data, and a deep-reasoning specialist.",
    tools=[
        genie_tool("space-abc"),
        vector_search_tool("main.search.docs_index",
                           columns=["doc_id", "title", "content"],
                           num_results=5),
        sql_tool(warehouse_id="wh-prod"),
        foundation_model_tool("databricks-claude-opus-4-7",
                              name="ask_opus",
                              description="Ask the specialist for hard reasoning."),
    ],
)
```

### UC functions as tools — `uc_function_tool` and `uc_function_toolkit`

`uc_function_tool` is the unlock for governed data teams. UC functions are already how data teams write and govern business logic — they define parameter types, write documentation, and apply access controls through standard UC governance. With `uc_function_tool`, the UC function *is* the tool definition.

```sql
-- Data team writes & registers the function in UC
CREATE OR REPLACE FUNCTION main.tools.classify_intent(query STRING)
RETURNS STRING
COMMENT 'Classify a customer query as: billing, technical, account, other.'
LANGUAGE PYTHON
AS $$
  # ... implementation
$$;

GRANT EXECUTE ON FUNCTION main.tools.classify_intent TO `agent_consumers`;
```

```python
# AI engineer wires it into the agent in one line
from apx_agent import Agent, uc_function_tool

agent = Agent(tools=[
    uc_function_tool("main.tools.classify_intent"),
])
```

The function's `COMMENT` becomes the tool description; parameter types become the tool schema. The user's grants on `main.tools.classify_intent` apply at runtime.

When the data team has curated an entire schema of agent-facing functions, register the whole toolkit at once:

```python
from apx_agent import Agent, uc_function_toolkit
from databricks.sdk import WorkspaceClient

agent = Agent(
    instructions="Triage customer queries using the curated tools.",
    tools=uc_function_toolkit("main.agent_tools", ws=WorkspaceClient()),
)
```

Use `include=[...]` and `exclude=[...]` to bound the surface when a schema mixes agent-facing tools with internal helpers.

---

## Agents as tools — `agent_tool`

`agent_tool` wraps any agent as a callable tool for another agent. The parent LLM decides when to delegate; the wrapped agent runs its full loop and returns a result. This is LLM-driven composition, as opposed to the fixed sequencing of `SequentialAgent` / `ParallelAgent`.

```python
from apx_agent import Agent, agent_tool

specialist = Agent(
    name="data_inspector",
    instructions="Inspect Unity Catalog lineage.",
    tools=[lineage_tool()],
)

orchestrator = Agent(
    instructions="Answer data questions. Delegate lineage lookups to the specialist.",
    tools=[
        agent_tool(specialist,
                   name="data_inspector",
                   description="Inspect Unity Catalog lineage for a table."),
    ],
)
```

Remote agents work identically — pass a `RemoteDatabricksAgent` instead of a local one:

```python
from apx_agent import RemoteDatabricksAgent, agent_tool

remote_billing = await RemoteDatabricksAgent.from_app_name("billing-agent")
orchestrator = Agent(tools=[agent_tool(remote_billing,
                                       name="billing",
                                       description="Answer billing questions")])
```

---

## Declared resources and `log_agent`

When the agent is logged to MLflow for Model Serving, its resource requirements are declared up front. Built-in tool factories attach their resources automatically. `log_agent` collects them from the full agent tree:

```python
from apx_agent import log_agent

log_agent(
    agent,
    model="databricks-claude-sonnet-4-6",
    registered_model_name="main.agents.data_triage",
)
# resources auto-derived from the agent tree:
#   DatabricksServingEndpoint("databricks-claude-sonnet-4-6")  # the LLM
#   DatabricksGenieSpace("abc123")                              # from genie_tool(...)
#   DatabricksFunction("main.tools.classify_intent")            # from uc_function_tool(...)
```

The platform enforces that the agent can only access the declared resources. For resources the framework can't infer automatically (a specific SQL warehouse, a UC table accessed from a raw SQL string), pass `extra_resources=[...]`:

```python
log_agent(
    agent,
    model="databricks-claude-sonnet-4-6",
    registered_model_name="main.agents.data_triage",
    extra_resources=[ResourceSpec("sql_warehouse", "wh-prod")],
)
```

---

## Declarative tools in `pyproject.toml`

Every built-in factory can be declared as data in `pyproject.toml` instead of code. The `type` key names the factory; the rest are its arguments:

```toml
[[tool.apx.tools]]
type = "genie"
space_id = "space-abc"

[[tool.apx.tools]]
type = "sql"
warehouse_id = "wh-prod"
```

Use code when the tool needs custom logic; use config for plain resource references you'd rather keep out of the agent module. See [`[[tool.apx.tools]]`](../reference/configuration.md#declarative-tools--toolapxtools) for the full schema.

---

## Controlling tool use

### Limit iterations

By default, the agent loops until it produces a final answer. Use `max_iterations` to cap the number of model+tool steps — useful when you want a hard ceiling on latency or cost. `None` leaves LangGraph’s default; any integer including `0` is an explicit cap (`0` → one hop, effectively no tool loop):

```python
agent = Agent(
    instructions="Answer the question using the tools provided.",
    tools=[sql_tool(warehouse_id="wh-prod"), genie_tool("space-abc")],
    max_iterations=5,
)
```

### Allow or deny specific tools

Use `ToolAllowlist` or `ToolDenylist` as a `before_tool` hook to restrict which tools the agent can invoke at runtime. This is a lighter-weight alternative to a Watchdog policy for a fixed, well-known set.

```python
from apx_agent import Agent, ToolAllowlist, ToolDenylist

# Only allow these tools to be called
agent = Agent(
    tools=[sql_tool(), genie_tool("space-abc"), lineage_tool()],
    before_tool=ToolAllowlist(["sql_tool", "genie_query"]),
)

# Or block specific tools
agent = Agent(
    tools=[sql_tool(), genie_tool("space-abc"), lineage_tool()],
    before_tool=ToolDenylist(["lineage_tool"],
                              message="Lineage lookups are disabled in production."),
)
```

`ToolAllowlist` and `ToolDenylist` raise `PermissionError` when the policy is violated; the model receives the rejection message and can adapt its response.

---

## What to read next

- [Custom tools](custom-tools.md) — `@tool` deep-dive, `http_tool`, `openapi_tool`, `mcp_tool`, authentication
- [MCP — Databricks Managed MCP](mcp.md) — expose your agent's tools to external clients
