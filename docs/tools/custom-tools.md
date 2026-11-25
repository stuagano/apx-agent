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

### Slow tools — a deadline of their own

Give a long-running tool its own wall-clock limit with `@cancellable(timeout_s=...)`, stacked under `@tool`. It works for sync and `async def` tools. Past the limit the call raises `ToolCancelled`, which apx returns to the model as an error tool message (`Error: … timed out after 300s`) — the turn continues and the agent can explain, never a 500. Report progress while it runs with `Dependencies.Progress`.

```python
from apx_agent import Dependencies, cancellable, tool

@tool
@cancellable(timeout_s=300)
async def sweep_scenarios(bank: str, progress: Dependencies.Progress) -> str:
    """Score every rate scenario for BANK."""
    for i, option in enumerate(OPTIONS):
        progress(f"scored {i + 1}/{len(OPTIONS)}")
        await score(bank, option)
    return summary()
```

For an `async def` tool the coroutine is really cancelled at the deadline. A sync tool runs in a worker thread that Python cannot kill; pass `canceller=` (e.g. a SQL statement cancel) to stop the remote work. For remote MCP tools use `mcp_tool(..., timeout_s=)` and, for servers that answer `in_progress`, `poll_tool=` (see [MCP](mcp.md)).

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

`output_key` stores final text by default. Pair it with `output_schema`, a
Pydantic model class, to publish validated JSON-compatible data and stop the
sequence on invalid output. See [typed step contracts](../agents/composition.md#typed-step-contracts).

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

When your tool accesses a specific Databricks asset, declare it so the agent's resource manifest includes it:

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

Calls an external HTTP service through the Unity Catalog **HTTP connection proxy**. The tool sends the request to

```
https://<workspace-host>/api/2.0/unity-catalog/connections/<connection>/proxy/<path>
```

with the calling user's Databricks token (OBO). Databricks authenticates the caller, checks `USE CONNECTION`, strips the Databricks `Authorization` header, injects the external service's credential stored in the connection, and forwards the request to `{connection host}{base_path}{path}`. The third-party token lives only in the connection: it never appears in `agent.py`, the app env, or the request the app sends. No SQL warehouse is involved.

> **Prefer a Databricks-provided MCP when one exists.** For Slack, GitHub, Gmail, and other systems that have a managed `system.ai.*` connector, use [`unity_gateway_mcp`](mcp.md#unity-gateway-mcp-services-systemai) instead of hand-describing REST operations. Use `http_tool` / `openapi_tool` for APIs that only have a UC HTTP connection.

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

`query` becomes URL query parameters on the proxy URL; `body` is sent as JSON with `Content-Type: application/json`. The tool returns `{"status_code": int, "text": str}` (non-2xx responses pass through, so the LLM sees the external error), or `{"error": str}` when the request cannot be sent.

**Signature:**

```python
http_tool(
    connection: str,
    *,
    method: str = "GET",          # GET, POST, PUT, DELETE, PATCH
    path: str = "",
    name: str = "http_request",
    description: str | None = None,
    warehouse_id: str | None = None,   # deprecated, ignored
    max_chars: int = 8000,
) -> tool
```

Each `http_tool` covers one fixed operation. To expose multiple operations from one API, create multiple `http_tool`s or use `openapi_tool`.

`connection` goes into the proxy path as a single percent-encoded segment, so a schema-level three-part name such as `main.connections.slack` is sent as `.../connections/main.connections.slack/proxy/...`. (The Databricks docs show only bare names in proxy URLs; the three-part form has not been verified against a live workspace yet.)

### Schema-level vs metastore-level connections

- **Schema-level** UC HTTP connections are **generally available**. Prefer them.
- **Metastore-level** connections are **Public Preview**. They still work with `http_tool`, but treat them as preview for production Apps.

### Declaring the connection resource

`http_tool` declares its UC HTTP connection as a `uc_connection` resource. `apx-agent agents deploy` compiles it into a Databricks App resource:

```yaml
- name: weather-api-<hash>
  uc_securable:
    securable_full_name: weather_api
    securable_type: CONNECTION
    permission: USE_CONNECTION
```

That is the same resource as **App resources → + Add resource → UC connection → Use Connection** in the Apps UI, and it grants the app's service principal `USE CONNECTION`. The tool also requests the `catalog.connections` user-authorization scope so the forwarded user token can reach the proxy. Each calling user still needs their own `USE CONNECTION` grant, because the request runs as that user. A custom tool that calls a connection some other way declares it with `attach_resources`:

```python
from apx_agent import ResourceSpec, attach_resources

attach_resources(my_tool, [ResourceSpec("uc_connection", "weather_api")])
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

After creating the connection, grant every principal that calls it — the users
of the app, and the app's service principal — permission to use it:

```sql
GRANT USE CONNECTION ON CONNECTION `weather_api` TO `<principal>`;
```

For a Databricks App deployment, the generated resource declaration requests
the `USE_CONNECTION` grant for the app's service principal; confirm the app
resource grant is approved before testing the tool. For local runs, the calling
user needs `USE CONNECTION`. No SQL warehouse is needed.

### Migrating from `http_request()` SQL

Earlier releases ran `http_tool` through the `http_request()` SQL function on a
SQL warehouse. Databricks now documents that path, and UC functions that wrap
`http_request()`, as legacy. `http_tool` now always uses the connection proxy:
drop any `warehouse_id=` argument (it is ignored with a `DeprecationWarning`).
The proxy also supports connection types that `http_request()` blocks, such as
OAuth U2M Per User.

### Ungoverned HTTP

For local experiments or a public API with no credential, a plain `@tool` with
its own `httpx` client is fine. Production Apps should route third-party calls
through a UC HTTP connection and `http_tool`: a token read from app env or
`agent.py` is visible to everyone who can read the app, and App Space apps have
no public internet egress at all.

---

## `openapi_tool` — OpenAPI spec → many tools

Turns every operation in an OpenAPI spec into a separate `http_tool`. Mirrors how `uc_function_toolkit` expands a Unity Catalog schema. Every generated tool calls its operation through the connection proxy for `connection`. The spec's `paths` are appended after the connection's `base_path`, so set `base_path` to the spec's server path prefix. Path templates such as `/users/{id}` are sent literally; the tools do not fill them in.

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
    warehouse_id: str | None = None,   # deprecated, ignored
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

For `http_tool` and `openapi_tool`, credentials are stored in a Unity Catalog HTTP connection and never appear in application code. The tool sends the calling user's Databricks token to `/api/2.0/unity-catalog/connections/<connection>/proxy/<path>`; Databricks checks `USE CONNECTION` and injects the connection's credential before forwarding the request.

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
| Slack, GitHub, Gmail, or another system with a `system.ai.*` connector | `unity_gateway_mcp` |
| One governed HTTP call (known endpoint) | `http_tool` |
| Full REST API via OpenAPI spec | `openapi_tool` |
| One tool from an MCP server you don't own | `mcp_tool` |
| All tools from an MCP server | `mcp_toolkit` |
