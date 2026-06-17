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
| Orchestrate / swap coding agents (Claude Code, Codex, Cursor) | an agent-orchestration / meta-harness framework |
| Sandbox dev work, gate shell/spend on a dev machine | an agent-orchestration / meta-harness framework |

## See also

- [agents/overview.md](agents/overview.md) — agent types and how to choose
- [design/okf-grounding-substrate.md](design/okf-grounding-substrate.md) — the open-format
  grounding substrate
- [tools/overview.md](tools/overview.md) — the governed tool primitives
- [get-started/migration.md](get-started/migration.md) — coming from ADK or the OpenAI Agents SDK
