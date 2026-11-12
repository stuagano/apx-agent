---
name: apx-migrate-langgraph
description: Wrap this project's existing LangGraph graph in an apx GraphAgent, verified by `apx-agent agents migrate-langgraph . --check`.
---

# Migrate a LangGraph app to apx

Run the existing graph as an apx `GraphAgent`: the **unchanged graph** gains apx-**governed tools** (permission
limits, guards, approvals, error containment), crash recovery, and serving on Databricks `DurableAgentServer`.

**Done** = `apx-agent agents migrate-langgraph . --check` exits 0. Its Request row may be WARN for an agent that
takes typed input; that is expected.

Reference implementation: `python/examples/langgraph-migration/after-apx/` in the apx-agent repo.

## Steps

1. **Factory.** Find where the graph is built and compiled (`builder.compile(...)`). Expose it as
   `build(tools, checkpointer)` that returns the compiled graph, compiled with the `checkpointer` argument
   (recovery and sessions depend on it). Keep every node, edge and reducer as they are. Done when one function
   returns the compiled graph for given tools and checkpointer.
   If the graph creates its own chat model, add a third `model` parameter, `build(tools, checkpointer, model)`,
   and use it in place of the graph's own model (`model.bind_tools(tools)` where it bound tools). apx passes the
   agent's configured model (`APX_MODEL`), and `--check` can then run the graph offline with a scripted model.
2. **Governed tools.** List every tool the graph binds: `ToolNode([...])`, `create_agent(tools=...)`,
   `.bind_tools(...)`, including inside subgraphs. Make each a plain Python function with typed parameters and a
   docstring, pass them as `GraphAgent(tools=[...])`, and have `build` bind the `tools` it receives. A tool that
   needs a workspace or SQL client declares a `Dependencies.Workspace` / `Dependencies.Sql` parameter; apx injects
   it per request. A tool you deliberately keep outside governance goes in `ungoverned=["name"]`. Done when every
   bound tool is one of the received `tools` or named in `ungoverned`.
3. **State and I/O.** The graph state needs a `messages` channel. Caller-supplied keys beyond messages (a tenant,
   an ID) become `input_schema` (a pydantic model with `extra="forbid"`) plus `input=lambda data: {...}` mapping
   onto the graph's own keys. Results callers read beyond messages become `output=lambda state: {...}`. Done when
   every field of the old endpoint's request and response maps to `input`, `output`, or messages.
4. **`agent.py`.** At the project root, define `agent = GraphAgent(build=..., tools=[...], name="...")` plus
   `input_schema` / `input` / `output` from step 3. Use a short kebab-case `name` (it becomes the app name).
5. **`pyproject.toml`.** Add dependencies `apx-agent[agentbricks]` and `databricks-agentbricks[langgraph]>=0.9.0,<0.10`
   (Agent Bricks' doctor requires the second explicitly). apx settings live under `[tool.apx.deploy]`, e.g.
   `name = "<name>"`.
6. **Entrypoint.** The old HTTP server is replaced: `--check` writes `server.py`, `app.yaml`, `agent.toml` and
   `.agentbricks/project.toml`, keeping any of those you already wrote. Stop starting the old server; keep its
   module if other code imports it.
7. **Check.** Run `apx-agent agents migrate-langgraph . --check`. Fix the first failing row, re-run, and repeat
   until it exits 0.

Report the files you changed, the final `--check` output, and the decisions below.

## Decisions for the human

Surface these; each needs a person, not a default.

- **Client contract.** Callers move from the old route to `POST /api/invocations` with a top-level `session_id`
  (the old thread ID) and typed input as a data part.
- **Identity.** Derive any tenant or actor from the verified caller (the forwarded identity), not from a body field.
- **Side effects.** Recovery is at-least-once: make writes and notifications idempotent (e.g. `MERGE` on a run ID).
- **History.** Existing checkpoints and transcripts start fresh in the managed Session Store; copying them is a
  separate job.
