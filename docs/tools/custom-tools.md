# Custom tools

How to define tools your agent can call — from plain Python functions to governed external services.

> `@tool` is equivalent to `@function_tool` in the OpenAI Agents SDK and `FunctionTool` in ADK. Type hints become the LLM-visible parameter schema; the docstring becomes the tool description.

| Factory | What it wraps |
|---------|--------------|
| `@tool` | Python function → agent tool |
| `agent_tool` | Any `BaseAgent` → callable tool for another agent |
| `http_tool` | Unity Catalog HTTP connection → one API operation |
| `openapi_tool` | OpenAPI spec + UC connection → many `http_tool`s |
| `mcp_tool` | Remote MCP server → one named tool |
| `mcp_toolkit` | Remote MCP server → all its tools |

For pre-built platform tools (`sql_tool`, `genie_tool`, `vector_search_tool`, `uc_function_tool`), see [tools/overview.md](overview.md). For exposing your agent's tools to external MCP clients, see [tools/mcp.md](mcp.md).

---

## `@tool` — function tools

Decorate any Python function to make it an agent tool. The function's type hints become the LLM-visible parameter schema; the docstring becomes the tool description.

```python
from apx_agent import tool

@tool
def get_order_status(order_id: str) -> str:
    """Return the current status of an order."""
    return db.query("SELECT status FROM orders WHERE id = ?", order_id)
```

Pass it to your agent:

```python
from apx_agent import Agent

agent = Agent(
    instructions="You are an order support assistant.",
    tools=[get_order_status],
)
```

The `@tool` decorator is optional — a plain function works the same way when passed to `tools=[...]`. Use the decorator when you want to override the name or description, or when you intend to publish the tool to UC.

### Make tool use explicit

The model decides whether to call a tool. If an answer must be grounded in a
service or table, say so in the agent instructions and make the tool
description specific about when it must be called:

```python
agent = Agent(
    instructions=(
        "For order status, always call get_order_status before answering. "
        "Never invent an order status when the tool has not returned one."
    ),
    tools=[get_order_status],
)
```

Use the **Traces** view to confirm the expected tool call and its result. If a
step must run regardless of model choice, use a `SequentialAgent` or call the
Python function directly; a tool exposed to an LLM is a model-selected action,
not a guaranteed workflow edge.

### Override name and description

Use the parameterized form to control what the LLM sees without changing the function code:

```python
@tool(
    name="lookup_order",
    description="Look up the current status of a customer order by ID.",
)
def get_order_status(order_id: str) -> str:
    ...
```

### Injected context — `Dependencies.*`

Tools running inside a Databricks App receive context from FastAPI via parameter injection. Declare injected parameters with `Dependencies.*` type aliases — the framework excludes them from the LLM's input schema and populates them at call time.

```python
from apx_agent import tool, Dependencies

@tool
def recent_orders(customer_id: str, ws: Dependencies.Workspace) -> list[dict]:
    """Return the 10 most recent orders for a customer."""
    return ws.statement_execution.execute_statement(
        f"SELECT * FROM main.sales.orders WHERE customer_id = '{customer_id}' LIMIT 10"
    )
```

Available injected types:

| Alias | What you get |
|-------|-------------|
| `Dependencies.Workspace` | `WorkspaceClient` authenticated as the **current user** (OBO token) |
| `Dependencies.Client` | `WorkspaceClient` using the app's service principal |
| `Dependencies.UserClient` | `WorkspaceClient` authenticated as the **current user** (OBO token) |
| `Dependencies.Sql` | SQL runner bound to the current user |
| `Dependencies.Principal` | Current user's username string, or `None` in local dev |
| `Dependencies.Progress` | Callable to emit a progress marker into the trace |
| `Dependencies.Request` | Raw FastAPI `Request` object |
| `Dependencies.State` | Dict-like state shared by tools and composition steps in this invocation |

`Dependencies.Workspace` is the most common choice — it passes the calling user's identity through to UC, SQL warehouses, and Genie spaces.

### Apps execution identity

For Apps deployments, APX compiles each tool to exactly one credential
identity. `Dependencies.Client` selects the App's platform-created service
principal; `Dependencies.UserClient`, `Dependencies.Workspace`, and
`Dependencies.Sql` select the requesting user's OBO credentials. The App has
one persistent platform-created service principal; an App family can share
access-group policy but never service-principal credentials.

Use `execution="user"` or `execution="service"` on `@tool` when a closure or
background operation cannot reveal its credential use in the function
signature. `build_tool(..., execution=...)` accepts the same override. An
override must agree with an injected credential dependency, and a tool that
mixes user and service credential dependencies is rejected; split it into two
auditable tools instead.

Request context is independent of credential identity. `Dependencies.Headers`,
`Dependencies.Principal`, and `Dependencies.Request` can provide request or
audit context to a service tool without converting its Databricks credentials
to OBO. `Dependencies.Progress` and `Dependencies.State` select neither
identity.

### Share state within an invocation

`Dependencies.State` is a dict-like, per-invocation view that lets tools pass
values to later graph steps. The state parameter is hidden from the model's
tool schema.

```python
from apx_agent import Agent, Dependencies, SequentialAgent, tool

@tool
def resolve_account(name: str, state: Dependencies.State) -> str:
    """Resolve an account and save its ID for a later tool."""
    account_id = lookup_account(name)
    state["account_id"] = account_id
    return account_id

@tool
def load_account_notes(state: Dependencies.State) -> str:
    """Load notes for the account resolved earlier in this invocation."""
    return fetch_notes(state["account_id"])

pipeline = SequentialAgent([
    Agent(tools=[resolve_account]),
    Agent(tools=[load_account_notes]),
])
```

For agent-to-agent handoffs, set `output_key` on the producing agent and
reference that key in the downstream agent's instructions:

```python
planner = Agent(instructions="Create an execution plan.", output_key="plan")
executor = Agent(instructions="Execute this plan:\n{plan}", tools=[...])
pipeline = SequentialAgent([planner, executor])
```

State is available only during the current invocation; it is not cross-session
memory. Reassign values after changing them (`state["items"] = [*state.get("items", []), item]`) because in-place mutation is not tracked. State
merges are shallow last-write-wins, so concurrent writers should use distinct
keys or aggregate at the agent step. See the [state access design reference](../design/keyed-state-tool-access.md) for mechanics and limits.

### UC-syncable tools

Adding `uc=` registers the function in Unity Catalog at deploy time. This makes the tool callable from Genie spaces, Managed MCP, and other agents browsing the catalog.

```python
@tool(
    uc="main.agents.tools.classify_intent",
    grant=["account users"],
)
def classify_intent(query: str) -> str:
    """Classify a customer query as billing/technical/account/other."""
    ...
```

`grant` sets `EXECUTE` on the UC function for the listed principals. It is only valid when `uc=` is set.

**Constraint:** UC-syncable tools cannot use `Dependencies.*` parameters. UC functions run server-side under the function owner's identity, so user-scoped clients are unavailable. Tools needing the calling user's identity stay Python-only.

Publish all UC-syncable tools in the agent tree at once:

```python
from apx_agent import publish_tools_to_uc

publish_tools_to_uc(agent)   # idempotent
```

### Declaring resources on a custom tool

When your tool accesses a specific Databricks asset, declare it so `log_agent` can include it in the Model Serving manifest:

```python
from apx_agent import ResourceSpec, attach_resources

@tool
def query_orders(question: str, ws: Dependencies.Workspace) -> str:
    """Query the orders Delta table."""
    return ws.statement_execution.execute_statement(
        "SELECT * FROM main.sales.orders ..."
    )

attach_resources(query_orders, [ResourceSpec("uc_table", "main.sales.orders")])
```

---

## `agent_tool` — agents as tools

`agent_tool` wraps any `BaseAgent` as a callable tool. The parent agent's LLM decides when to invoke it; the wrapped agent runs its full loop and returns a response string. This is LLM-driven delegation, as opposed to the fixed sequencing of `SequentialAgent` / `ParallelAgent`.

```python
from apx_agent import Agent, agent_tool, lineage_tool

specialist = Agent(
    name="data_inspector",
    instructions="Inspect Unity Catalog lineage and schema.",
    tools=[lineage_tool()],
)

orchestrator = Agent(
    instructions="Answer data questions. Use the specialist for lineage lookups.",
    tools=[
        agent_tool(specialist,
                   name="data_inspector",
                   description="Inspect table lineage and schema in Unity Catalog."),
    ],
)
```

Remote agents work identically:

```python
from apx_agent import RemoteDatabricksAgent, agent_tool

remote_billing = await RemoteDatabricksAgent.from_app_name("billing-agent")
orchestrator = Agent(
    tools=[agent_tool(remote_billing,
                      name="billing",
                      description="Answer billing and invoice questions.")]
)
```

**Signature:**

```python
agent_tool(
    agent: BaseAgent,
    *,
    name: str | None = None,        # defaults to snake_case of agent class/name
    description: str | None = None,
) -> Callable
```

---

## `http_tool` — governed external HTTP

Calls an external HTTP service through a Unity Catalog HTTP connection. The request runs server-side via Databricks' `http_request` SQL function under the calling user's identity — credentials live in the UC connection and are never seen by the app.

```python
from apx_agent import Agent, http_tool

fetch_weather = http_tool(
    "weather_api",           # UC HTTP connection name
    method="GET",
    path="/v1/current",
    name="get_current_weather",
    description="Fetch current weather for a given city.",
)

agent = Agent(tools=[fetch_weather])
```

At call time the LLM can pass two optional inputs:

| Input | Type | Description |
|-------|------|-------------|
| `query` | `dict[str, str]` | URL query parameters appended to the request |
| `body` | JSON dict | Request body (for POST/PUT/PATCH) |

The tool returns `{"status_code": int | None, "text": str}`.

**Signature:**

```python
http_tool(
    connection: str,
    *,
    method: str = "GET",
    path: str = "",
    name: str = "http_request",
    description: str | None = None,
    warehouse_id: str | None = None,
    max_chars: int = 8000,
) -> tool
```

Each `http_tool` covers one fixed operation. To expose multiple operations from one API, create multiple `http_tool`s or use `openapi_tool`.

### Declaring the connection resource

In `pyproject.toml`, declare the UC HTTP connection so `apx-agent agents deploy` wires the `USE_CONNECTION` grant:

```toml
[[tool.apx.agent.resources]]
type = "uc_connection"
name = "weather_api"
```

### Creating and granting the connection

The connection must exist in Unity Catalog before the app can use it. A
metastore admin (or a principal with `CREATE CONNECTION`) can create an HTTP
connection in Catalog Explorer, through the Databricks SQL editor, or with the
Connections API. The exact authentication fields depend on the external
service; keep tokens and OAuth credentials in the connection, never in
`agent.py`.

See the authoritative [Connect to external HTTP services](https://docs.databricks.com/aws/en/query-federation/http)
guide for the supported authentication modes and `CREATE CONNECTION` examples.

After creating the connection, grant the deployed app identity (or the user
running locally) permission to use it:

```sql
GRANT USE CONNECTION ON CONNECTION `weather_api` TO `<principal>`;
```

Then declare the same connection name in `pyproject.toml` as above. For a
Databricks App deployment, the generated resource declaration requests the
`USE_CONNECTION` grant; confirm the app resource grant is approved before
testing the tool. For local runs, the calling user needs `USE CONNECTION` and
the SQL warehouse used by the tool must be available.

---

## `openapi_tool` — OpenAPI spec → many tools

Turns every operation in an OpenAPI spec into a separate `http_tool`. Mirrors how `uc_function_toolkit` expands a Unity Catalog schema.

```python
from apx_agent import Agent, openapi_tool

tools = openapi_tool(
    "https://petstore3.swagger.io/api/v3/openapi.json",
    connection="petstore_api",
)

agent = Agent(tools=tools)
```

`spec` can be a URL (`http://` / `https://`) or a local file path. JSON and YAML are both supported (YAML requires `pyyaml`).

Use `include` to keep only the operations you need:

```python
tools = openapi_tool(
    spec="./specs/payments_api.yaml",
    connection="payments_api",
    include=["charge", "refund"],   # keep only ops whose path/operationId contains these
)
```

**Signature:**

```python
openapi_tool(
    spec: str,
    connection: str,
    *,
    warehouse_id: str | None = None,
    include: list[str] | None = None,
) -> list[tool]
```

---

## `mcp_tool` / `mcp_toolkit` — consume external MCP servers

These are client-side wrappers — they call out to a remote MCP server rather than serving one. For exposing your agent's tools as an MCP server to external clients, see [tools/mcp.md](mcp.md).

### `mcp_tool` — one named tool

Wraps a single named tool from a remote MCP server. The remote tool's description is fetched at factory time (best-effort) so the LLM sees useful documentation.

```python
from apx_agent import Agent, mcp_tool

search = mcp_tool(
    "https://search.example.com/mcp",
    "web_search",
    description="Search the web and return top results.",   # optional override
)

agent = Agent(tools=[search])
```

For MCP servers running in the same Databricks workspace, the calling user's OBO token is forwarded automatically — no `headers` needed.

For third-party servers, pass static auth headers:

```python
search = mcp_tool(
    "https://api.example.com/mcp",
    "web_search",
    headers={"X-Api-Key": os.environ["EXAMPLE_API_KEY"]},
)
```

**Signature:**

```python
mcp_tool(
    server_url: str,
    tool_name: str,
    *,
    headers: dict[str, str] | None = None,
    transport: str = "http",   # "http" (streamable) or "sse"
    name: str | None = None,
    description: str | None = None,
) -> tool
```

### `mcp_toolkit` — all tools from a server

Discovers every tool the server advertises and wraps each one. Use when you want the full surface of a remote MCP server without listing tools by name.

```python
from apx_agent import Agent, mcp_toolkit

tools = mcp_toolkit("https://tools.example.com/mcp")

agent = Agent(tools=tools)
```

Filter to a subset:

```python
tools = mcp_toolkit(
    "https://tools.example.com/mcp",
    include=["search", "summarize"],   # keep tools whose name contains any of these
)
```

**Signature:**

```python
mcp_toolkit(
    server_url: str,
    *,
    headers: dict[str, str] | None = None,
    include: list[str] | None = None,
    transport: str = "http",
) -> list[tool]
```

---

## Authentication

### OBO token passthrough (Databricks-to-Databricks)

When calling MCP servers or HTTP connections inside the same Databricks workspace, the calling user's OBO token is forwarded automatically. No extra configuration is needed.

### Static headers (third-party services)

For external APIs that use API keys or bearer tokens, pass them via the `headers` parameter:

```python
# mcp_tool
tool = mcp_tool("https://api.example.com/mcp", "tool_name",
                headers={"Authorization": f"Bearer {os.environ['API_TOKEN']}"})

# mcp_toolkit
tools = mcp_toolkit("https://api.example.com/mcp",
                    headers={"X-Api-Key": os.environ["API_KEY"]})
```

### Governed credentials via UC HTTP connections

For `http_tool` and `openapi_tool`, credentials are stored in a Unity Catalog HTTP connection and never appear in application code. The request runs under the calling user's identity via Databricks' `http_request` SQL function.

```python
# Credentials stored in UC — not in code
fetch = http_tool("my_api_connection", method="GET", path="/data")
```

---

## Decision guide

| Need | Use |
|------|-----|
| Business logic in Python | `@tool` |
| Access current user's workspace | `@tool` with `Dependencies.Workspace` |
| Sync tool to UC catalog | `@tool(uc=..., grant=[...])` |
| All UC functions in a schema | `uc_function_toolkit(schema)` |
| LLM-driven delegation to another agent | `agent_tool(agent)` |
| One governed HTTP call (known endpoint) | `http_tool` |
| Full REST API via OpenAPI spec | `openapi_tool` |
| One tool from an MCP server you don't own | `mcp_tool` |
| All tools from an MCP server | `mcp_toolkit` |
