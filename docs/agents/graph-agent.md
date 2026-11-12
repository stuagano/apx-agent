# GraphAgent: host an existing LangGraph graph

`GraphAgent` runs a LangGraph graph you already wrote, **unchanged**, as an apx agent. apx calls your graph
factory with **governed tools**: each one gets the same permission limits, guards, approvals and error
containment an `LlmAgent`'s tools get. The agent then serves on the durable target (`DurableAgentServer`)
with sessions, crash recovery and A2A.

Use it when you have a hand-written graph with its own nodes, state and routing. To build a new agent, start
with [`Agent` / `LlmAgent`](llm-agent.md) or a [composition](composition.md).

```python
from pydantic import BaseModel, ConfigDict

from apx_agent import Dependencies, GraphAgent
from rca.graph import build_rca_graph  # your existing factory


def query_kpis(tenant: str, ws: Dependencies.Workspace) -> dict:
    """Return the tenant's KPI values."""
    ...


class RcaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str


agent = GraphAgent(
    build=lambda tools, checkpointer: build_rca_graph(tools=tools, checkpointer=checkpointer),
    tools=[query_kpis],
    name="rca",
    input_schema=RcaInput,
    input=lambda data: {"tenant": data["tenant"]},           # onto the graph's own state keys
    output=lambda state: {"anomalies": state["anomalies"]},  # into the response's "output"
)
```

## What apx adds to your graph

| You write | apx provides |
|---|---|
| `build(tools, checkpointer)` returning a compiled graph | Called at compile time with governed `tools` and the run's checkpointer |
| Plain tool functions in `tools=[...]` | Injected `Dependencies` (hidden from the model), request identity, `ToolScope` limits |
| `before_tool` / `after_tool` / a `PolicyGate` | Hooks run inside every tool call in your `ToolNode`; `ASK` pauses for approval and resumes |
| Nothing | `PermissionError`, `ToolCancelled` and `ToolError` become an error tool result (`Error: …`), so the turn continues; genuine bugs still fail loud |
| `input_schema` + `input=` | Typed caller input, validated, mapped onto your graph's own keys |
| `output=` | The response's `output`, from your graph's final state |
| `name=` | Session threads keyed by agent, user and session; crash recovery that resumes at the unfinished node |
| `session_budget={"tokens": N, "usd": D}` | A cumulative per-session cap, enforced at turn boundaries as on `LlmAgent` |
| Nothing | The agent card and MCP list your tools (plus `/tools/<name>` routes on hosts that mount them; the durable target mounts none) |

## Rules

- **The graph needs a `messages` channel**, and `build` must return the compiled graph itself (`.with_config()` on
  it is fine). apx inspects it, so an opaque wrapper such as `RunnableLambda` is refused.
- **Every tool the graph binds must come from `tools`.** Compile walks every `ToolNode`, including in subgraphs and
  `create_agent` graphs, and refuses a tool that isn't one of the governed objects `build` received. A raw tool
  that reuses a governed tool's name is refused too. For a deliberate exception, name it in
  `ungoverned=["local_math"]`. A node that calls a raw function directly, outside a `ToolNode`, can't be detected.
- **Compile `build` with the `checkpointer` argument.** Sessions, approvals and recovery depend on it.
- **A `session_budget` needs a `state: dict` key in the graph's state schema.** apx keeps the session's running
  total there. Compile refuses a budgeted graph without it, because otherwise the cap would silently never
  accumulate.
- **Recovery is at-least-once.** A crashed run resumes at the unfinished node, so make that node's side effects
  idempotent.

## Migrating an existing project

```bash
apx-agent agents migrate-langgraph .          # writes apx-migrate/SKILL.md and prints a prompt for your coding agent
apx-agent agents migrate-langgraph . --check  # verifies the result
```

The skill walks a coding agent through the steps above. `--check` loads the project's `GraphAgent`, compiles it
(running the ungoverned-tool check), writes `server.py`, `app.yaml`, `agent.toml` and `.agentbricks/project.toml`
without overwriting yours, then runs `apx-agent doctor --durable` and the real `agentbricks doctor`. It exits 1 on
any failure. For a typed agent, its Request row is a WARN: the offline probe can't supply real typed input.

A worked before/after is in
[`python/examples/langgraph-migration`](../../python/examples/langgraph-migration/): `after-apx/` keeps
`rca/graph.py` byte-identical to `before/`.

History: #1037 (parts A–C3: #1152, #1153, #1155, #1157, #1158).
