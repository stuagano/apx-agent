---
name: adk-tool-design
description: Apply Google ADK tool-design best practices to apx-agent tools. Use when writing a tool for an agent, when a tool is not being called or is called with wrong arguments, when deciding how a tool should return or handle errors, when designing a long-running tool, or when choosing between a hand-written @tool and a governed UC function tool.
---

# ADK tool design

ADK's tool best practices, mapped onto apx-agent's `@tool` and UC-governed
tool surface. Judgment first; link to docs for syntax.

## The docstring and type hints ARE the contract

apx-agent derives the LLM-visible parameter schema from the function's type
hints and the tool description from its docstring. That means tool design *is*
signature-and-docstring design:

- **Verb-first name** the LLM can map to intent (`lookup_order`, not `orders`).
- **Docstring says when to call and what it returns**, not how it's implemented.
- **One clear job per tool.** A tool that does three things is a tool the LLM
  calls at the wrong time. Split it.
- Override the surface with `@tool(name=..., description=...)` when the
  function's own name/docstring aren't what the LLM should see.

A tool "not being called" or "called wrong" is almost always a description
problem, not a model problem. Fix the contract first.

## Prefer the governed path

apx-agent's differentiator over vanilla ADK: the UC function *is* the tool.

- **`uc_function_tool` / `uc_function_toolkit`** when the logic belongs to a
  data team — the UC `COMMENT` becomes the description, parameter types become
  the schema, and UC grants apply at runtime. Use `include=`/`exclude=` to
  bound a mixed schema.
- **`@tool(uc=..., grant=[...])`** + `publish_tools_to_uc(agent)` to publish a
  Python tool to UC so Genie, Managed MCP, and other agents can call it.

Reach for a plain `@tool` only when the logic genuinely doesn't belong in UC.

## Returns and errors

- **Return something structured the model can act on** — a value or dict, not a
  stringified blob it must re-parse.
- **Handle expected failures inside the tool** and return a usable message
  ("no order found for id X") rather than raising blind. Raising is for
  guardrails (see `adk-safety-callbacks`), not for normal not-found paths.

## Bound the surface

- **`max_iterations`** on the agent caps the model+tool loop — a hard ceiling
  on latency/cost.
- **`Dependencies.*`** parameters (Workspace, Client, Sql, Principal, Progress,
  Request) are injected by the framework and excluded from the LLM schema — use
  them instead of asking the model for context it shouldn't supply. Note:
  UC-syncable tools cannot use `Dependencies.*`.
- **Declare resources** with `attach_resources(tool, [ResourceSpec(...)])` when
  a tool touches a specific asset from a raw SQL string, so `log_agent` builds
  a correct Model Serving manifest. Built-in factories declare theirs
  automatically.

## See also

- Wrapping a whole agent as a tool: `agent_tool` in `adk-multi-agent`.
- Blocking or filtering tool calls at runtime (`before_tool`/`after_tool`,
  `ToolAllowlist`/`ToolDenylist`): `adk-safety-callbacks`.

## Read next

- [Tools overview](../../../docs/tools/overview.md) — `@tool`, built-in
  factories, `uc_function_tool`, `Dependencies.*`, `log_agent`, `max_iterations`.
- [Custom tools](../../../docs/tools/custom-tools.md) — `@tool` deep-dive,
  `http_tool`, `openapi_tool`, `mcp_tool`, authentication.
- [MCP](../../../docs/tools/mcp.md) — expose tools to external clients.
