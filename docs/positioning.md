# Where apx-agent fits

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
- **Declared, not wired.** One Python object or a `[tool.apx.agent]` TOML block becomes a
  working, observable, governed agent; apx-agent normalizes the LLM API formats, memory
  backends, conversation history, and trace schemas underneath.

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
| Multi-agent orchestration (supported but not demonstrated) | `SequentialAgent`, `ParallelAgent`, `RouterAgent`, composition |
| Authoring (write a `ResponsesAgent`, wrap your framework) | declare a `[tool.apx.agent]` block or a Python object; apx-agent compiles it and normalizes the LLM, memory, and trace formats |

In short: apx-agent is a batteries-included, governed, data-grounded toolkit over the same
primitives the GA framework exposes — the way an opinionated framework sits over a lower-level
one. Use the raw framework when you want maximum control over a custom `ResponsesAgent`; use
apx-agent when you want a governed, UC-grounded data agent without wiring the grounding,
data-plane tools, identity passthrough, and memory yourself.

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
