# Custom tools

How to define tools your agent can call — from plain Python functions to governed external services.

| Factory | What it wraps |
|---------|--------------|
| `@tool` | Python function → agent tool |
| `http_tool` | Unity Catalog HTTP connection → one API operation |
| `openapi_tool` | OpenAPI spec + UC connection → many `http_tool`s |
| `mcp_tool` | Remote MCP server → one named tool |
| `mcp_toolkit` | Remote MCP server → all its tools |

For pre-built governed primitives (`sql_tool`, `genie_tool`, `vector_search_tool`, `uc_function_tool`), see [tools/overview.md](overview.md). For consuming Databricks Managed MCP, see [tools/mcp.md](mcp.md).

---

## `@tool` — Python function tools

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
    model="databricks-claude-sonnet-4-6",
    instructions="You are an order support assistant.",
    tools=[get_order_status],
)
```

### Override name and description

Use the parameterized form to override what the LLM sees without touching the function code:

```python
@tool(
    name="lookup_order",
    description="Look up the current status of a customer order by ID.",
)
def get_order_status(order_id: str) -> str:
    ...
```

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

**Constraint:** UC-syncable tools cannot use `Dependencies.*` parameters (warehouse, HTTP connection, etc.) — those are for server-side-only tools.

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

Each `http_tool` call = one fixed operation. To expose multiple API operations, either create multiple `http_tool`s or use `openapi_tool`.

### Declaring the connection resource

In `pyproject.toml`, declare the UC HTTP connection so `apx-agent deploy` wires the `USE_CONNECTION` grant:

```toml
[[tool.apx.agent.resources]]
type = "uc_connection"
name = "weather_api"
```

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

These are *client-side* wrappers — they call out to a remote MCP server rather than serving one. For running an MCP server inside your agent, see [tools/mcp.md](mcp.md).

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

## Decision guide

| Need | Use |
|------|-----|
| Business logic in Python | `@tool` |
| Sync tool to UC catalog | `@tool(uc=..., grant=[...])` |
| One governed HTTP call (known endpoint) | `http_tool` |
| Full REST API via OpenAPI spec | `openapi_tool` |
| One tool from an MCP server you don't own | `mcp_tool` |
| All tools from an MCP server | `mcp_toolkit` |
