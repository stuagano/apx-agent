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
| **Advanced tools** | `http_tool`, `openapi_tool`, `mcp_tool`, `mcp_toolkit`, `unity_gateway_mcp`, `agent_tool` |

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

When your tool accesses a specific Databricks asset, declare it so the agent's resource manifest includes it:

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

These are pre-built tools for the Databricks platform. Each factory returns a ready-to-use tool (or list of tools) and automatically attaches the required resource declaration.

| Factory | What it does | Resource declared |
|---------|--------------|-------------------|
| `uc_function_tool(name)` | Execute a registered UC function with a `params` dictionary. UC types guide SQL literal conversion. | `DatabricksFunction` |
| `uc_function_toolkit(schema)` | All agent-facing functions in a UC schema as tools | `DatabricksFunction` (×N) |
| `genie_tool(space_id)` | Ask a natural-language question to a Genie space; follow-ups in one session continue the same conversation | `DatabricksGenieSpace` |
| `genie_agent_tool(space_id)` | Ask a Genie Agent in Agent mode; streams reasoning and SQL as trace progress and returns the cited report (`structured=True` for a dict) | `DatabricksGenieSpace`, `genie` user scope |
| `genie_one_tool()` | Ask Genie One (workspace-wide Genie) via its managed MCP; asks and polls in one call, as the calling user, and returns the finished answer or a deep link after `wait_s` | `genie` user scope |
| `sandbox_tool(sandbox_id=None, auth="user")` | **Beta.** Run model-generated Python or shell in a Databricks Sandbox and return exit code, stdout and stderr; the code never runs in the App process | `system.ai.sandbox` service binding (no Apps resource) |
| `unity_gateway_mcp(service, tools=None)` | Wrap a Unity Gateway MCP service (`system.ai.web_search`, `system.ai.dbsql`, `system.ai.sandbox`, `system.ai.genie_one_mcp`, connectors such as `system.ai.github`) as tools run as the calling user, governed by Unity Gateway. See [mcp.md](mcp.md#unity-gateway-mcp-services-systemai) | `ai-gateway` user scope, `ServiceBinding(service, "user")` (no Apps resource) |
| `vector_search_tool(index_name)` | Query a Vector Search index — top-k results, optional column projection | `DatabricksVectorSearchIndex` |
| `sql_tool(warehouse_id=...)` | Run arbitrary SQL against a SQL warehouse | `DatabricksSQLWarehouse` |
| `document_extract_tool(warehouse_id=..., volume=..., schema=...)` | Parse a UC volume file with `ai_parse_document` and extract a committed schema via `ai_extract` | `DatabricksSQLWarehouse` |
| `foundation_model_tool(model)` | Ask another model — agent-to-model routing. Takes a **Unity Gateway model service** `catalog.schema.service` (e.g. `system.ai.claude-sonnet-4-5`), or `databricks-<id>`, which becomes `system.ai.<id>`. Queried through `/ai-gateway/mlflow/v1` as the App service principal. Any other plain name is rejected. | `uc_securable` `MODEL_SERVICE` / `EXECUTE` (Apps resource, Public Preview) |
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
        foundation_model_tool("system.ai.claude-opus-4-7",   # Unity Gateway model service
                              name="ask_opus",
                              description="Ask the specialist for hard reasoning."),
    ],
)
```

A `foundation_model_tool` needs `EXECUTE` on the service (plus
`USE CATALOG` / `USE SCHEMA`) for the App service principal; deploy declares
the `MODEL_SERVICE` Apps resource that grants it. A Model Serving `CAN_QUERY`
grant does not grant access to a Unity Gateway service. See
[Unity Gateway model services](../deploy/unity-gateway-model-services.md) for
the preview status, privilege auto-grants, budgets, rate limits and usage.

### Genie follow-ups — `genie_tool` and `genie_query_tool`

Within one apx session (the principal-scoped `thread_id` / `session_id` the
turn runs under), `genie_tool` and `genie_query_tool` keep one Genie
conversation per space. The first question starts a conversation; later
questions in the same session go to it through
`ws.genie.create_message_and_wait`, so Genie keeps the context of earlier
questions ("and last year?"). Different sessions never share a conversation,
and a turn with no session starts a new conversation for each question, as
before. If the stored conversation no longer exists, the tool starts a new one.

apx deletes the conversations it started:

- when your app calls `end_genie_session(session_id)` at the end of a chat;
- when a conversation sits idle for an hour, or is the least recently used once
  more than 512 are tracked in the process.

Deletion is best-effort and never fails a user request. Turn follow-ups off per
tool with `genie_tool(space_id, follow_ups=False)`.

Conversations left behind by a stopped process can be swept. The sweep lists
the caller's conversations (or every user's with `--all-users` /
`include_all=True`, which needs CAN MANAGE on the space) created before the age
cutoff. It cannot tell apx-started conversations from ones a person started in
the Genie UI, so choose an age past anything still in use:

```bash
apx-agent resources genie-sweep SPACE_ID --older-than-hours 24 --dry-run   # list only
apx-agent resources genie-sweep SPACE_ID --older-than-hours 24             # delete
```

```python
from apx_agent import sweep_genie_conversations

stale = sweep_genie_conversations(ws, "space-abc", older_than_s=86400)          # dry run
deleted = sweep_genie_conversations(ws, "space-abc", older_than_s=86400, dry_run=False)
```

### Genie Agent mode — `genie_agent_tool`

`genie_agent_tool(space_id)` sends the prompt to the Genie Agent in
[Agent mode](https://docs.databricks.com/aws/en/genie-agents/conversation-api)
(`POST /api/2.0/genie/agents/{space_id}/responses`) as the calling user (OBO).
Agent mode plans the work, runs several SQL queries and any MCP tools attached
to the Genie Agent, and writes a report with citations. It can take minutes.

- **Progress.** The answer streams over Server-Sent Events. Each finished
  reasoning step and each SQL query is recorded as an `apx.progress` event on
  the tool's trace span (the slow-tool progress from #1116), so the dev UI trace
  shows the steps while the call runs. Without an active trace the tool still
  returns the final report.
- **Result.** By default the tool returns the report text followed by its
  sources. `structured=True` returns a dict with `report`, `citations` (the
  report's annotations as Genie sends them), `queries` (each `call_id`, its
  `sql`, and its `result` summary), `tables`, `conversation_id`, `response_id`,
  and `status`.
- **Follow-ups.** One Agent mode conversation is continued per apx session per
  space, kept apart from the Chat mode conversations of `genie_tool`, and
  deleted the same way (`end_genie_session`, idle, or LRU eviction).
  `follow_ups=False` starts a new conversation for every prompt.
- **Limits and errors.** `wait_s` (default 600) bounds the whole call. Past it
  apx closes the stream, cancels the response (closing alone does not cancel
  it), and the tool raises a `ToolError`. A failed response, a stream that ends
  early, a permission error, or a stale OBO consent (with the re-authorize hint)
  also become a `ToolError`, never a 500.

```python
from apx_agent import Agent, genie_agent_tool

agent = Agent(tools=[genie_agent_tool("space-abc", wait_s=900)])
```

Callers need CAN VIEW on the Genie Agent and `SELECT` on its data. The tool
declares the Genie space resource and the `genie` user scope.

### Code execution — `sandbox_tool`

`sandbox_tool()` lets an agent run Python or shell code in a
[Databricks Sandbox](https://docs.databricks.com/aws/en/compute/serverless/sandbox):
an isolated serverless Linux environment with 4 vCPUs and 8 GB RAM. The model
passes `code` and `language` (`"python"` runs `python3 -c`, `"shell"` runs
`bash -c`); the tool returns `exit_code`, `stdout`, `stderr` and `truncated`.
The code is sent to the Sandbox API and runs there, never in the App process.

```python
from apx_agent import Agent, sandbox_tool

agent = Agent(tools=[sandbox_tool()])                       # a new sandbox per call, as the user
analyst = Agent(tools=[sandbox_tool("scratch", wait_s=600)])  # one named sandbox, files persist
service = Agent(tools=[sandbox_tool(auth="app")])           # as the App service principal
```

**Beta.** Databricks Sandbox is in Beta. Databricks does not charge for it
during Beta, pricing may change, and Databricks may purge data stored in
sandboxes when Beta ends. To enable it:

1. Use a workspace in a
   [supported serverless region](https://docs.databricks.com/resources/feature-region-support#serverless-availability).
2. A workspace admin enables **Databricks Sandbox** on the workspace
   [Previews](https://docs.databricks.com/aws/en/admin/workspace-settings/manage-previews) page.
3. Only when you call `system.ai.sandbox` through Unity Gateway MCP instead
   ([`unity_gateway_mcp("system.ai.sandbox")`](mcp.md#systemaisandbox-and-sandbox_tool),
   or agent.toml `agentbricks tools add sandbox` with `platform_tools(...)`), an
   account admin also enables **Enhanced Unity Gateway** in the account console
   Previews page. `sandbox_tool` calls the Sandbox API directly and does not
   need it.

`apx-agent doctor --online` probes the Sandbox API for every declared
`sandbox_tool` and fails with these steps when the preview is off.

- **Identity.** `auth="user"` (default) runs as the calling user (OBO), so
  sandbox code can reach only what that user can. `auth="app"` runs as the App
  service principal, like `agentbricks tools add sandbox --auth app`.
- **Sandboxes.** With no `sandbox_id`, each call creates a sandbox, runs the
  code, and deletes the sandbox. With `sandbox_id="name"`, apx creates that
  sandbox on first use, starts it when it has stopped, and never deletes it, so
  files under `/home/sandbox-agent` persist between calls (other paths,
  including `/tmp`, are cleared when the sandbox stops).
- **Limits and errors.** `wait_s` (default 300) bounds the whole call: sandbox
  start plus the command, which the Sandbox API terminates at the deadline. A
  non-zero exit code is a result the model can read. A timed-out or failed
  command, a disabled preview (with the enable step), a permission error, or a
  stale OBO consent (with the re-authorize hint) become a `ToolError`, never a
  500. The tool returns the final result; it does not stream output.
- **Binding.** The tool declares `ServiceBinding("system.ai.sandbox", auth)`,
  the same service and identity an Agent Bricks `[[tools]]` sandbox entry
  binds. The service has no Apps resource, so it adds nothing to
  `databricks.yml`. There is no downscope ceiling yet: sandbox code runs with
  the identity's own Unity Catalog access (#1146).

Live execution needs a workspace with the preview on, so CI covers it with a
mocked Sandbox API. To check it by hand, run `apx-agent doctor --online`, then
ask the agent in `apx-agent dev` to run `print(2 + 2)` and confirm `stdout` is
`4` and the run's trace shows the sandbox progress events.

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

The function's baked `COMMENT` becomes the tool description; without a baked signature, pass `ws=` to fetch the comment at construction time (otherwise a generic description is used). The model-facing input is a generic `params` dictionary, not a per-parameter schema derived from UC types. UC signatures provide parameter names, order, and types for SQL literal conversion at execution time. Unknown supplied parameter names are rejected, and BOOLEAN values must be JSON booleans or null. Omitted parameters retain the existing behavior of being passed as SQL `NULL`, not omitted to invoke UC defaults. Scaffold, deploy, and `apx-agent agents refresh-schema` bake those signatures into `.apx/schema.json`, so deployed Apps execute known functions through SQL without a request-time Unity Catalog metadata lookup. Projects without a baked signature fall back to live metadata discovery. The user's grants on `main.tools.classify_intent` still apply at runtime.

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

## Declared resources

An agent's resource requirements are declared up front. Built-in tool factories attach their resources automatically, and apx collects them from the full agent tree and emits them with the app's deployment:

```text
DatabricksServingEndpoint("databricks-claude-sonnet-4-6")  # the LLM
DatabricksGenieSpace("abc123")                              # from genie_tool(...)
DatabricksFunction("main.tools.classify_intent")            # from uc_function_tool(...)
```

The declared resources become the App's resources and OAuth scopes at deploy, so the App is granted exactly those. For resources the framework can't infer automatically (a specific SQL warehouse, a UC table accessed from a raw SQL string), declare them on the tool with `attach_resources(tool, [ResourceSpec("sql_warehouse", "wh-prod")])`.

---

## Python-native tool layout

Declare tools with the existing Python factories in an ordinary `tools/` package,
then import them explicitly in `agent.py`. Group related tools by domain; module
names are organizational, not tool identities. There is no directory-scanning
loader, per-tool TOML schema, or required directory for every tool kind.

```text
my-agent/
├── agent.py                 # Explicit imports and agent composition
├── tools/
│   ├── __init__.py           # May be empty
│   └── sales.py              # Factory calls or custom @tool functions
├── pyproject.toml           # Packaging, [tool.apx.deploy], deployment settings
└── agent.toml               # Agent Bricks settings (auth, stores, platform tools)
```

```python
# tools/sales.py
import os

from apx_agent import genie_tool, sql_tool, vector_search_tool

ask_account_data = genie_tool(
    os.environ["ACCOUNT_GENIE_SPACE_ID"],
    name="ask_account_data",
    description="Ask a question about a customer's account.",
)
search_docs = vector_search_tool(
    "main.search.docs_index",
    columns=["doc_id", "title", "content"],
    num_results=5,
)
query_sales = sql_tool(warehouse_id=os.environ["SQL_WAREHOUSE_ID"])
```

```python
# agent.py
from apx_agent import Agent
from tools.sales import ask_account_data, query_sales, search_docs

agent = Agent(
    instructions="Answer questions using account data and documentation.",
    tools=[ask_account_data, search_docs, query_sales],
)
```

Factories still attach governed resource metadata; moving their calls into a
module does not change resource collection. For toolkit factories that return a
list, expand it in composition: `tools=[ask_account_data, *function_tools]`.
Python imports execute trusted project code; some factories discover remote
metadata at construction time. This layout is not static/no-execution loading.

### Migrating legacy tool TOML

`[[tool.apx.tools]]` runtime authoring has been removed. Its presence now fails
with migration guidance, including in mixed Python/TOML projects; entries are
not silently ignored or merged.

1. Replace each tool table with the corresponding factory call in `tools/`.
   Preserve its tool name, description, resource references, and governance.
2. Resolve environment values explicitly, for example `os.environ["SQL_WAREHOUSE_ID"]`;
   Python string literals containing `$VAR` are not automatically expanded.
3. Import the callables in `agent.py` and pass them to `Agent(tools=[...])`, or
   `DataAgent` / `CoworkerAgent(extra_tools=[...])`.
4. Remove the legacy tool tables. Keep deployment
   configuration in `pyproject.toml` and runtime settings on the agent in `agent.py`. `agent.toml` holds Agent Bricks settings,
   not tool definitions.

For per-tool identity, UC ceilings, secret scopes, or caller restrictions, use
`build_tool(scope=ToolScope(...))` on the factory's returned callable. Do not
invent `identity=`, `callers=`, or `scope=` kwargs on individual factories:

```python
# tools/billing.py
from apx_agent import ToolScope, build_tool, uc_function_tool

create_refund = uc_function_tool("main.billing.refund_create")
create_refund = build_tool(
    create_refund,
    name="create_refund",
    description="Create a billing refund for an authorized caller.",
    scope=ToolScope(
        identity="obo",
        functions=("main.billing.refund_create",),
        callers=("alice@example.com", "billing-bot@example.com"),
    ),
)
```

`build_tool` annotates the same callable, preserving the factory's attached
resources when `resources` is omitted. `ToolScope(secret_scopes=("billing",))`
can also declare required secret scopes. Scope ceilings do not grant access:
the selected principal still needs the corresponding Databricks permissions.
See [configuration](../reference/configuration.md#python-authored-tools) for more
factory examples and [MCP caller restrictions](mcp.md#restricting-a-tool-to-specific-callers).

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

Use `ToolAllowlist` or `ToolDenylist` as a `before_tool` hook to restrict which tools the agent can invoke at runtime. This is a lighter-weight alternative to a governance-monitoring policy for a fixed, well-known set.

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
