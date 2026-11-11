# ADR 0001: Build on Agent Bricks, not next to it

- **Status:** Accepted
- **Date:** 2026-11-11

## Context

Agent Bricks ships a CLI (`agentbricks`) and an SDK (AgentKit, `databricks_agentkit`). AgentKit provides `DurableAgentServer`, managed memory and session stores, tool bindings and deploy.

apx already compiles agents to this stack: it emits `agent.toml`, runs on `DurableAgentServer`, and deploys through the real `agentbricks` CLI. It also keeps parallel versions of several things the stack provides: a second runtime, its own Lakebase stores, its own memory pipeline, its own tool-scope check and its own dev server.

Parallel paths drift. Moving from 0.5 to 0.9 broke apx tests in three places (#1128).

## Decision

1. **apx compiles to a standard Agent Bricks project.** Every apx agent emits a valid `agent.toml` and a `DurableAgentServer` entrypoint. The real `agentbricks doctor` must pass on it, and `agentbricks deploy` must ship it unchanged. A conformance test enforces this.
2. **apx extensions never add keys to `agent.toml`.** Composition, `sub_agents` / A2A, typed contracts, grounding, templates, fleet operations and governance checks live in apx's own declaration. They compile to generated code and library calls on `DurableAgentServer`.
3. **`DurableAgentServer` is the only runtime.** The `responses_agent` target is deprecated now and will be removed after one release.
4. **Managed stores are the only durable backend for new agents.** `inmemory` stays for local dev and tests. apx's Lakebase memory, conversation and checkpoint stores, and its own memory extraction and consolidation, are deprecated in favour of managed stores and `agentbricks memory pipeline`. Memory still defaults to `off`, and Lakebase is never required.
5. **Tool scope compiles to the platform where it can.** For platform tools, declared scope becomes `[[tools]]` `auth` and `policy.downscope`, which the platform enforces. `ToolScope` and `ScopeGuard` remain only for custom Python tools, which Agent Bricks does not cover.
6. **Local dev runs on the Agent Bricks dev runtime.** `apx-agent dev` runs the agent with the same command and environment as `agentbricks dev`. apx's `/_apx` pages mount as extra routes on the same `DurableAgentServer` app.
7. **Existing Lakebase agents keep working through the deprecation.** `apx-agent doctor` warns about them, scaffolding stops emitting them, and a doc covers moving to managed stores. There is no automatic history migration, which matches upstream behaviour.

## Consequences

- An apx feature that duplicates an Agent Bricks capability needs a written reason. The default is to call the upstream surface.
- An Agent Bricks upgrade can break only apx's generated code, never an apx-specific manifest, and the conformance test catches it.
- apx's own surface shrinks to what Agent Bricks lacks: multi-agent composition with per-hop identity, typed contracts, grounding, templates, fleet operations, governance checks and the dev pages.

## Follow-up

| Issue | Decision |
|---|---|
| #1133 Conformance test with the real `agentbricks doctor` | 1 |
| #1134 Deprecate `responses_agent` | 3 |
| #1135 Deprecate apx Lakebase stores | 4, 7 |
| #1136 Compile tool scope to `[[tools]]` | 5 |
| #1137 Run `apx-agent dev` on the Agent Bricks dev runtime | 6 |
| #1129 Store permissions, #1131 Prompt Registry bindings, #1132 load testing | Use the upstream surface (1, 2) |
| #1130 Model and prompt tuning | Call AgentKit `model_upgrades`; `label align` supplies the judge |
