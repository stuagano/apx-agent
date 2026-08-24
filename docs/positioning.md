# Where apx-agent fits

> **Build governed Databricks data agents without wiring the platform by hand.**

Define your agent's tools, data access, identity, memory, and policies once, then run it on the
Databricks runtime that fits your workload.

apx-agent is **infrastructure for building and serving governed data agents on Databricks**.
You declare what an agent should be; apx-agent compiles it to a Databricks runtime, grounds it
in your Unity Catalog data, runs its tools under UC governance, and makes it observable.

The agent-tooling ecosystem is broad, and several categories of tool are easy to confuse with
apx-agent because they share words like "agent," "framework," and "governance." This page
explains what apx-agent is for — and what it is *not* for — so you can tell when to reach for
it and when to reach for something else.

## What apx-agent is for

Use apx-agent when you want a **production data agent**:

- **Grounded in your data.** The agent knows its tables, columns, and semantics from an
  [open-format knowledge bundle](design/okf-grounding-substrate.md) auto-generated from Unity
  Catalog — no `SHOW TABLES` discovery step, no hand-maintained prompt.
- **Governed by Unity Catalog.** Its tools (`sql_tool`, `genie_tool`, `uc_function_tool`,
  `vector_search_tool`) run under UC grants and **end-user identity passthrough** — the agent
  sees only what the asking user is permitted to see. Even metadata *writes* (e.g. the
  `uc_comment_writer` tool) run as the calling user under their grants and are audited.
- **Deployed to Databricks.** Targets Databricks Apps and model serving, with sessions,
  semantic memory, and MLflow tracing wired from the declaration.
- **Declared, not wired — for one agent and for many.** One Python object or a
  `[tool.apx.agent]` TOML block becomes a working, observable, governed agent; apx-agent
  normalizes the LLM API formats, memory backends, conversation history, and trace schemas
  underneath. The same declaration scales to a **fleet**: `sub_agents=[url]` and A2A let agents
  call each other across apps, with the caller's identity passed through per hop so downstream
  *tools* still run under the asking user's UC grants. A callee's own LLM (FMAPI) calls run as
  that app's service principal — see [the fleet section below](#a-governed-fleet-not-just-one-agent)
  and [multi-agent/a2a.md](multi-agent/a2a.md).

Canonical examples are `DataAgent` (one line over a UC schema) and `CoworkerAgent` (join two
source systems on a shared key) — see [agents/overview.md](agents/overview.md).

## apx-agent and the Databricks Agent Framework

apx-agent builds **on** the official
[Databricks Mosaic AI Agent Framework](https://docs.databricks.com/aws/en/generative-ai/agent-framework/author-agent),
not beside it. It uses the same GA primitives — MLflow `ResponsesAgent` / `ChatAgent` served by
the MLflow `AgentServer`, packaged and deployed through a Databricks asset bundle to Databricks
Apps or Model Serving. An apx-built agent **is** a GA-compliant agent, so adopting apx-agent
keeps you on the official path.

What apx-agent adds is the layer the GA authoring workflow leaves to the developer:

| The GA workflow leaves to the developer | apx-agent provides |
|---|---|
| Retrieval / grounding (implement via MCP or custom tools) | open-format grounding auto-generated from Unity Catalog — the agent knows its tables and columns |
| Built-in UC data tools (connect via MCP / custom endpoints) | `sql_tool`, `genie_tool`, `uc_function_tool`, `vector_search_tool` — built-in and governed |
| End-user identity passthrough (manual `get_user_workspace_client()`) | identity passthrough wired declaratively; tools run as the asking user, and metadata writes run under their grants |
| Memory / state backends (not configured for you) | Lakebase / UC managed semantic memory and sessions, declared |
| Multi-agent orchestration (structural primitives, no cross-app runtime or governed-per-hop story) | `SequentialAgent` / `ParallelAgent` / `RouterAgent` / `HandoffAgent` locally, **plus `sub_agents=[url]` + A2A across apps with the caller's identity passed through per hop for tool calls** (callee LLM calls use the callee's service principal) — shipped and demonstrated (`data-triage-agent` over A2A, `customer_triage` handoffs) |
| Authoring (write a `ResponsesAgent`, wrap your framework) | declare a `[tool.apx.agent]` block or a Python object; apx-agent compiles it and normalizes the LLM, memory, and trace formats |

In short: apx-agent is a batteries-included, governed, data-grounded toolkit over the same
primitives the GA framework exposes — the way an opinionated framework sits over a lower-level
one. Use the raw framework when you want maximum control over a custom `ResponsesAgent`; use
apx-agent when you want a governed, UC-grounded data agent without wiring the grounding,
data-plane tools, identity passthrough, and memory yourself.

## A governed fleet, not just one agent

Single-agent grounding and governance is the on-ramp. The payoff is that the *same*
declaration composes into a fleet without changing how governance works.

- **Local composition** — `SequentialAgent`, `ParallelAgent`, `LoopAgent`, `RouterAgent`,
  `HandoffAgent` compose agents in one process when they share a lifecycle and have no external
  caller.
- **Remote composition (A2A)** — when a sub-agent has its own consumers, deploy cadence, or
  scaling profile, it lives in its own app and is reached with `sub_agents=[url]`. Every
  deployed agent serves an A2A discovery card at `/.well-known/agent.json`; siblings find each
  other by probe. `apx-agent doctor` reports per-sub-agent reachability.
- **Governed per hop, at the tool boundary** — the cross-app call uses the app-to-app auth path
  and forwards the caller's OBO token, so a downstream agent's *tools* run under the *asking
  user's* UC grants, not a shared service principal. What does **not** cross the boundary is the
  callee's model access: an A2A callee's own LLM (FMAPI) calls run as that app's service
  principal, because each Databricks App authenticates outbound model traffic with its own
  credentials. Data access stays user-scoped per hop; model access is app-scoped. See
  [multi-agent/a2a.md](multi-agent/a2a.md) for the auth path and this caveat in full (#633).

This is adjacent to Databricks
[Agent Services](https://docs.databricks.com/aws/en/ai-gateway/agent-services) (Beta), a
registration, discovery, and governance surface. Its current documentation is transitional:
one section describes `EXECUTE` invocation while the limitations section says runtime invocation
is unavailable. Do not assume that registering an agent creates a supported runtime call path.
apx-agent's A2A runtime is separate; the two may compose where the workspace release supports
both paths, but verify runtime invocation before making it a customer promise.

Two examples ship this end-to-end: `data-triage-agent` (a 6-step `SequentialAgent` delegating to
a `data-inspector` sub-agent over A2A) and `customer_triage` (a `HandoffAgent` over four
specialists with memory recall surviving each handoff — Apps deploy verified live on
`fe-stable`). See [multi-agent/overview.md](multi-agent/overview.md) for the local-vs-remote
decision matrix and [multi-agent/a2a.md](multi-agent/a2a.md) for the discovery card and
app-to-app auth.

## By hand vs. declared: a worked comparison

Here is the same agent — a support analyst grounded in a Vector Search index with two tools
(KB search + account lookup) — written two ways. The hand-written version is a typical
raw-SDK notebook: ~220 lines across config, tool functions, hand-authored tool schemas, an
agentic loop, tracing wrappers, and a served model. The apx-agent version is a declaration.

**By hand (abridged — the real notebook is ~220 lines):**

```python
# config: WorkspaceClient, token plumbing, OpenAI client, autolog ... (~15 lines)

def search_knowledge_base(query, num_results=3):
    res = w.vector_search_indexes.query_index(index_name=VS_INDEX,
        columns=["doc_id", "category", "content"], query_text=query, num_results=num_results)
    return "\n\n---\n\n".join(f"[{r[0]}]\n{r[2]}" for r in res.result.data_array)

def lookup_account(identifier): ...  # spark.table(...).where(...) — runs as the notebook user

TOOLS = [ {"type": "function", "function": {"name": "search_knowledge_base",
    "parameters": {...hand-written JSON schema...}}}, {...second schema...} ]

@mlflow.trace(name="support_agent")
def run_agent(user_message, max_turns=6):
    messages = [{"role": "system", ...}, {"role": "user", "content": user_message}]
    for _ in range(max_turns):                 # hand-rolled tool-calling loop
        rsp = client.chat.completions.create(model=LLM, messages=messages, tools=TOOLS)
        msg = rsp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls: return msg.content
        for tc in msg.tool_calls:              # tool_call_id bookkeeping
            result = TOOL_FN_MAP[tc.function.name](**json.loads(tc.function.arguments))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

# ...then ~90 more lines: both tools AND the loop re-implemented inside a PythonModel,
#    written to a temp .py, infer_signature, log_model with pinned pip_requirements.
```

**Declared (apx-agent — the whole thing):**

```python
from apx_agent import LlmAgent, vector_search_tool, uc_function_tool

agent = LlmAgent(
    instructions="You are the Meridian Support Analyst...",
    tools=[
        vector_search_tool(
            "catalog.schema.knowledge_corpus_index",
            columns=["doc_id", "category", "content"], num_results=3,
        ),
        uc_function_tool("catalog.schema.lookup_account"),
    ],
    max_iterations=6,
)
```

```bash
apx-agent agents deploy meridian.yaml --target apps   # or --target model-serving
```

What each step costs, side by side:

| Step | By hand (raw SDK notebook) | apx-agent |
|---|---|---|
| **Ground** | `query_index(...)` call + manual result-row unpacking | `vector_search_tool(index, columns=…, num_results=…)` |
| **Tools** | Two functions **+ two hand-authored OpenAI JSON schemas** | `vector_search_tool(…)`, `uc_function_tool(…)` — schemas introspected from the tool |
| **Loop** | Hand-rolled `run_agent`: `max_turns`, `tool_call_id` bookkeeping, `model_dump(exclude_none=True)` | runtime-owned; you set `max_iterations` |
| **Trace** | `@mlflow.trace` + `with mlflow.start_run(...)` wrappers | automatic MLflow tracing on both targets |
| **Evaluate** | `eval_data` golden set + an `agent_fn` adapter + `mlflow.evaluate(model_type="databricks-agent")` | golden set stays; the adapter and scaffold go — `apx-agent eval` |
| **Ship** | **~90 lines**: tools + loop re-implemented inside a `PythonModel`, temp `.py`, `infer_signature`, pinned `pip_requirements` | `apx-agent agents deploy` |
| **Govern** | `lookup_account` runs as the **notebook user** via `spark.table` | tools run under the **calling user's** UC grants (OBO), audited |

**Net: ~220 lines → ~15 lines + a TOML block, roughly 90% less code.** The parts that disappear
are the drift-prone ones: the raw notebook maintains the loop and both tools *twice* — once to
demo interactively, once re-implemented inside the logged `PythonModel` — which is the single
biggest source of prototype-vs-production skew. apx-agent serves the same object you ran locally.

Two honest caveats:

1. **`lookup_account` isn't a free swap.** The notebook filters a table with
   `spark.table(...).where(...)`; to use `uc_function_tool` you first register a UC function
   (a few lines of SQL), or use [`sql_tool`](tools/overview.md) instead. Either way the lookup
   becomes governed and runs as the asking user — which the notebook version does not.
2. **Evaluation isn't eliminated, just relocated.** You still author the eval golden set — that
   is real domain work, not boilerplate. apx-agent removes the `agent_fn` adapter and the
   serving/logging scaffold around it, not the dataset.

## Deploy anywhere, light up the right Databricks tools

The promise is **one agent definition, deployed to whichever Databricks target the
tools you need require** — not one deployment that magically appears everywhere. The
same declared agent compiles to either [Databricks Apps or Model
Serving](deploy/apps-vs-model-serving.md); switching is a `--target` flag, not a
rewrite. `apx-agent agents deploy --target apps` runs the bundle path;
`apx-agent agents deploy --target model-serving` runs `log_agent` +
`databricks.agents.deploy`.

What that unlocks across the rest of the Databricks agent ecosystem follows one rule:
**how does a given tool find your agent?** Two families, two answers.

- **Trace/experiment-keyed tools** read the MLflow traces your agent emits. apx-agent
  wires tracing on both targets, so these work no matter where you deploy.
- **Serving-endpoint-keyed tools** call your agent by a Model Serving endpoint *name*.
  They only light up when the agent **is** a serving endpoint — i.e. deployed
  `--target model-serving`. This is a Databricks-side contract, not an apx-agent
  limitation: those tools have no way to dial a Databricks App URL.

| Databricks tool | Finds your agent via | Apps | Model Serving |
|---|---|:---:|:---:|
| [Agent Evaluation](evaluate/overview.md) (`mlflow.genai.evaluate`) | traces / `predict_fn` | ✅ | ✅ |
| Review App **labeling sessions** (review existing traces) | traces in the experiment | ✅ | ✅ |
| MLflow trace UI / monitoring | traces in the experiment | ✅ | ✅ |
| AI Playground | serving endpoint name | ❌ | ✅ |
| Review App **chat UI** (live interactive testing) | serving endpoint name | ❌ | ✅ |
| Mosaic AI Supervisor routing | serving endpoint name | ❌ | ✅ |

So the practical guidance is: **deploy `--target model-serving` when your release process
includes Playground, the Review App chat UI, or Supervisor routing; deploy `--target apps`
when you want fast iteration, a co-located UI, async/WebSocket work, or per-app
governance** — and either way, evaluation, trace monitoring, and human trace-review (via
labeling sessions) work unchanged. The one path that is Model-Serving-only is *live*,
chat-driven review; on Apps the supported substitute is a labeling session over captured
traces, which is also Databricks' own recommendation. See
[deploy/apps-vs-model-serving.md](deploy/apps-vs-model-serving.md) for the full
target-by-target comparison.

## What apx-agent is not for

apx-agent is **not** a coding-agent orchestrator or a cross-harness meta-framework. If your
goal is to:

- supervise or swap between coding agents (Claude Code, Codex, Cursor, and similar),
- sandbox local development work or gate shell/file access and spend on dev machines, or
- run inner-loop developer automation across multiple agent harnesses,

then you want an **agent-orchestration / meta-harness** tool, not apx-agent. Projects in that
category (for example, [omnigent](https://github.com/omnigent-ai/omnigent)) operate at a
different layer: they orchestrate *coding* agents and enforce *process* policies (shell/file
access, spend caps, tool-call limits, OS sandboxing). They generally have no Unity Catalog
grounding or governed data-plane access — that is apx-agent's layer.

## Complementary, not competing

These layers compose. An apx-built agent can be one of the agents an orchestration layer
supervises; an orchestration layer can manage a fleet of coding agents while apx-agent builds
the data coworkers those teams deploy. Reach for apx-agent for the **data agent + Databricks
governance** layer, and for an orchestration framework for the **coding-agent + process-policy**
layer.

## Quick guide

| You want to… | Reach for |
|---|---|
| Build a data agent grounded in a UC schema | **apx-agent** (`DataAgent` / `CoworkerAgent`) |
| Serve a governed agent on Databricks Apps / model serving | **apx-agent** |
| Have tools run as the asking user under UC grants | **apx-agent** |
| Hand-author a custom `ResponsesAgent` with maximum control | the Databricks Agent Framework directly (apx-agent builds on it) |
| Orchestrate / swap coding agents (Claude Code, Codex, Cursor) | an agent-orchestration / meta-harness framework |
| Sandbox dev work, gate shell/spend on a dev machine | an agent-orchestration / meta-harness framework |

## See also

- [agents/overview.md](agents/overview.md) — agent types and how to choose
- [design/okf-grounding-substrate.md](design/okf-grounding-substrate.md) — the open-format
  grounding substrate
- [tools/overview.md](tools/overview.md) — the governed tool primitives
- [deploy/apps-vs-model-serving.md](deploy/apps-vs-model-serving.md) — picking a deploy
  target and what each one unlocks
- [evaluate/overview.md](evaluate/overview.md) — evaluation and trace-based review
- [get-started/migration.md](get-started/migration.md) — coming from ADK or the OpenAI Agents SDK
