# Governed primitives

## UC functions are the unlock

UC functions are already how data teams write and govern business logic. They define parameter types, write documentation, and apply access controls through standard UC governance. Without a UC function tool, an AI engineer duplicates that work by hand-writing a tool schema and a call implementation that mirrors what the data team already registered. The two definitions then drift apart.

With `uc_function_tool`, the UC function *is* the tool definition. The data team owns the logic; the AI engineer registers it in one line. Governance, access control, and documentation flow through UC the same way they do for any other data asset. Data teams ship new agent capabilities through their normal workflow — write SQL or Python, register in UC, done — without touching agent code.

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
# AI engineer composes the agent
from apx_agent import Agent, uc_function_tool

agent = Agent(tools=[
    uc_function_tool("main.tools.classify_intent"),
])
```

When the agent runs, the user's grants on `main.tools.classify_intent` apply. If they can't execute it directly, the agent can't either. The function's `COMMENT` becomes the tool description; parameter types become the tool schema. One source of truth.

## Author tools that live in UC — `@tool`

When the agent author *is* the one writing the tool, `@tool` lets you author the Python function once and have it land in UC with one publish step. Type hints become parameter types; docstring becomes the function comment; `grant=[...]` enforces who can execute.

```python
from apx_agent import Agent, tool, log_agent, publish_tools_to_uc

@tool(uc="main.tools.classify_intent", grant=["agent_consumers"])
def classify_intent(query: str) -> str:
    """Classify a customer query as billing/technical/account/other."""
    return "billing" if "bill" in query.lower() else "other"

agent = Agent(
    instructions="Triage the user's question.",
    tools=[classify_intent],
)

publish_tools_to_uc(agent)   # registers + grants in UC, idempotent
log_agent(agent, model="databricks-claude-sonnet-4-6",
          registered_model_name="main.agents.triage")
```

After `publish_tools_to_uc`, `main.tools.classify_intent` exists as a governed UC asset. Genie reaches it, Managed MCP exposes it, sibling agents wire it in one line via `uc_function_tool("main.tools.classify_intent")` — without redefinition. The Python function still runs in-process when *this* agent calls it; the UC function is the discovery and external-composition surface.

### Pulling a whole schema of UC functions as tools — `uc_function_toolkit`

When the data team has curated a schema of agent-facing UC functions, register the entire toolkit in one line. Each function's UC `comment` becomes the tool description; parameter types come from UC; the `DatabricksFunction` resource declaration is attached automatically.

```python
from apx_agent import Agent, uc_function_toolkit
from databricks.sdk import WorkspaceClient

agent = Agent(
    instructions="Triage customer queries using the curated tools.",
    tools=uc_function_toolkit("main.agent_tools", ws=WorkspaceClient()),
)
```

`include=[...]` and `exclude=[...]` bound the surface when the schema mixes agent-facing tools with internal helpers. The toolkit returns an empty list (with a warning) if listing fails — typically a UC permissions issue, surfaced loudly so it doesn't slip past in a deploy script.

Three rules locked in:

1. **UC-syncable iff pure.** `@tool(uc=...)` is rejected at definition time if the function has a `Dependencies.*` parameter — UC functions run server-side under the function owner, so user-scoped `WorkspaceClient` is unavailable. Tools that need the calling user's identity (lineage lookups, Genie calls, UC reads) stay Python-only.
2. **Explicit, three-part UC names.** `catalog.schema.function`. No implicit namespacing.
3. **Declarative grants.** `grant=[...]` is the source of truth; `publish_tools_to_uc` enforces.

Requires the `uc` extra. apx-agent isn't on PyPI yet — install from a git clone:

```bash
git clone https://github.com/stuagano/apx-agent.git
cd apx-agent/python
pip install -e '.[uc]'
```

## Platform tool factories

| Factory | What it does | Resource declared |
|---------|--------------|-------------------|
| `uc_function_tool(name)` | Execute a registered UC function. Schema auto-derived from UC. | `DatabricksFunction` |
| `genie_tool(space_id)` | Ask a natural-language question to a Genie space | `DatabricksGenieSpace` |
| `vector_search_tool(index_name)` | Query a Vector Search index — top-k results, optional column projection | `DatabricksVectorSearchIndex` |
| `sql_tool(warehouse_id=...)` | Run arbitrary SQL against a SQL warehouse. Returns rows + truncation flag | `DatabricksSQLWarehouse` *(if `warehouse_id` set)* |
| `foundation_model_tool(endpoint)` | Ask a Foundation Model endpoint — agent-to-model routing | `DatabricksServingEndpoint` |
| `lineage_tool()` | Get upstream/downstream lineage for a UC table | — *(UC REST gated by grants)* |
| `schema_tool()` | Describe columns of a UC table | — |
| `catalog_tool(catalog, schema)` | List tables in a UC schema | — |

Each factory attaches its resource declaration to the returned tool. `log_agent` collects them automatically.

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

## Declared resources

When the agent is logged to MLflow for Model Serving, its resources are declared up front:

```python
log_agent(
    agent,
    model="databricks-claude-sonnet-4-6",
    registered_model_name="main.agents.data_triage",
)
# resources auto-derived from the agent tree:
#   DatabricksServingEndpoint("databricks-claude-sonnet-4-6")  # the LLM
#   DatabricksGenieSpace("abc123")                              # from genie_tool(...)
#   DatabricksFunction("main.tools.classify_intent")            # from uc_function_tool(...)
#   DatabricksServingEndpoint("billing")                        # from sub_agents=[...]
```

The platform enforces that the agent can **only** access those resources. Need to declare something the framework can't infer (a specific SQL warehouse, vector index, UC table)? Pass `extra_resources=[ResourceSpec("sql_warehouse", "wh-prod"), ...]`.
