# Future: Databricks AI Bridge Integration

> **Status: Scoped out — `databricks-ai-bridge` / `databricks-langchain` already provides this.**
>
> This doc captures the design intent so it doesn't get lost, but implementation is deferred.

## Context

The Databricks AI ecosystem already ships first-class integrations for the patterns below via [`databricks-ai-bridge`](https://pypi.org/project/databricks-ai-bridge/) and `databricks-langchain`:

| Integration | Provided by |
|-------------|-------------|
| `ChatDatabricks` — OpenAI-compatible client for serving endpoints | `databricks-langchain` |
| `GenieAgent` — conversational agent backed by a Genie space | `databricks-langchain` |
| `DatabricksVectorSearch` — retriever for Vector Search indexes | `databricks-langchain` |
| Unity Catalog function tools | `databricks-langchain` |

## What apx-agent could add (if needed later)

If users want these as ready-to-register tools rather than raw LangChain objects, apx-agent could expose thin factory helpers:

```python
from apx_agent.bridge import genie_tool, vector_search_tool

agent = Agent(tools=[
    genie_tool(space_id="abc123"),
    vector_search_tool(index="catalog.schema.index", num_results=5),
])
```

Each factory would return a typed `Tool` that wraps the corresponding Bridge class, so the LLM gets it as a normal apx-agent tool with an auto-generated schema.

## Relationship to apx-agent

apx-agent is complementary to, not a replacement for, the Bridge:

- apx-agent handles **orchestration** (agent loop, MCP, A2A, hub registration, dev UI)
- Bridge handles **platform integrations** (serving endpoints, Genie, Vector Search as LLM primitives)

A natural composition is an apx-agent app that uses Bridge tools alongside custom Python functions.

## Implementation notes (if we revisit)

- Add `databricks-langchain` as an optional dependency group (`[bridge]` extra)
- Factory helpers live in `src/apx_agent/bridge.py`
- `apx init --template genie` and `apx init --template rag` templates could use them
- Tests should mock the Bridge classes — no live workspace needed

## Decision

Deferred. The Bridge libraries are actively maintained by Databricks and cover this space well. Add apx-agent bridge helpers only if users ask for tighter integration that the raw libraries don't provide.
