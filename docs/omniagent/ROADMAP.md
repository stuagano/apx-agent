# apx-agent → omniagents Parity Roadmap

This roadmap identifies the gaps between apx-agent and omniagents and orders them by impact.
Where apx-agent already leads (semantic memory, ExampleStore, UC/Genie/SQL tools), those are
not listed — focus here is on what omniagents has that apx-agent lacks.

---

## Phase 1 — Core execution gaps

These are architectural limitations that constrain everything else.

### 1.1 Pluggable executor / harness

**Gap:** apx-agent compiles to LangGraph + LiteLLM. omniagents has an `Executor` protocol with
explicit harness selection (`claude-sdk`, `codex`, `openai-agents`, `databricks`,
`openai-responses`) and a clean event stream (`TextChunk`, `ToolCallRequest`, `TurnComplete`, etc.).

**Why it matters:** LangGraph is a heavy dependency and couples the agent loop to a specific
runtime. A harness abstraction makes it possible to swap in Claude SDK natively, run codex,
or target Databricks model serving without rewriting agents.

**Target shape:**
- Define an `Executor` protocol that emits typed events
- Implement `ClaudeSDKExecutor` and `DatabricksExecutor` as first two harnesses
- Keep LangGraph as an optional compilation path, not the default

---

### 1.2 Cancellable tools

**Gap:** apx-agent has no equivalent to `CancellableFunctionTool`. Long-running work (SQL
queries, file operations, web fetches) cannot be interrupted mid-flight.

**Why it matters:** Agents in multi-user deployments need to be cancellable — a user navigating
away or an admin kill should not leave orphaned subprocesses or open warehouse queries.

**Target shape:**
- `CancellableTool` wrapper that accepts a `runner` with an `interrupt()` method
- Wire cancellation signal through the executor event loop
- Expose `cancel_session` endpoint on the serving layer

---

## Phase 2 — Governance

### 2.1 Human-in-the-loop (ASK policy)

**Gap:** apx-agent guardrails can only ALLOW or DENY. omniagents adds `ASK` — execution
**pauses** and waits for a human approval before proceeding.

**Why it matters:** This is the primitive that makes agents safe for consequential actions
(writes, sends, deploys). Without it, the only safe posture is to block the tool entirely.

**Target shape:**
- Extend `PolicyResult` to `ALLOW | ASK | DENY`
- `ASK` suspends the current turn and emits an approval request event to the frontend
- Frontend renders an approve/reject UI; agent resumes on approval or raises on rejection
- Configurable timeout (approve-or-deny within N seconds, else DENY)

---

### 2.2 Rich policy system

**Gap:** apx-agent has `before_tool` / `after_tool` / `input_guardrail` hooks. omniagents
intercepts at four named phases (`request`, `response`, `tool_call`, `tool_result`) and
supports two policy types: `FunctionPolicy` (callable) and `PromptPolicy` (LLM classifier).

**Why it matters:** `PromptPolicy` lets you write a natural-language rule ("deny if the
assistant is about to send an email to an external domain") without wiring up Python callbacks.
The four-phase model makes it explicit where in the turn lifecycle each rule applies.

**Target shape:**
- Define `Policy` base with `on: list[phase]` and `evaluate() -> PolicyResult`
- Implement `FunctionPolicy` (wraps existing guardrail callables)
- Implement `PromptPolicy` (sends content to an LLM classifier with a rubric)
- Max-action composition: DENY beats ASK beats ALLOW
- Policies can attach `set_labels` to the session for audit trails

---

### 2.3 Session labels

**Gap:** apx-agent has no concept of runtime labels on sessions. omniagents policies can
call `set_labels: {"risk": "high", "topic": "finance"}` as a side effect.

**Why it matters:** Labels are how you build audit dashboards, trigger downstream workflows,
and gate on session properties without hardcoding logic into each policy.

**Target shape:**
- `Session` gets a `labels: dict[str, str]` field
- Policy evaluation can set/overwrite labels
- Labels exposed on the session API and in MLflow tracking

---

## Phase 3 — Authoring experience

### 3.1 Skills

**Gap:** omniagents has `SkillTool` — a `SKILL.md` file that gets loaded into context
on demand when the agent calls it. apx-agent has no equivalent.

**Why it matters:** Skills let you package reusable procedures (a research workflow,
a coding style guide, a data analysis playbook) as markdown and inject them only when
needed — keeping the base context window clean.

**Target shape:**
- `skills/<name>/SKILL.md` directory convention inside the agent bundle
- `SkillTool` that accepts a skill name and returns the markdown content as a tool result
- Skills listed in the agent config; auto-discovered from directory structure

---

### 3.2 YAML authoring path (partially done — extend)

**Status:** apx-agent already has a YAML spec path. `_yaml_spec.py` loads a `.yaml` file
into `AgentConfig` via `load_spec()`, resolves `$VAR` / `${VAR}` env references, and
validates with Pydantic. The `apx scaffold` command generates these files. Example:

```yaml
name: my-payroll
model: databricks-claude-sonnet-4-6
instructions: You are a clinical revenue cycle analyst...
template:
  name: coworker
  catalog: $CATALOG
  schema: $SCHEMA
  persona: a clinical revenue cycle analyst
  join_key: patient encounter
  objective: Identify claim denial root causes...
memory:
  type: delta
  table_name: $CATALOG.$SCHEMA.apx_my_payroll_memory
session:
  type: delta
  table_name: $CATALOG.$SCHEMA.apx_my_payroll_sessions
guardrails:
  injection_detection: true
```

**Remaining gaps vs. omniagents:**
- `tools:` in the YAML is currently stripped before Pydantic validation (`data.pop("tools", None)`) — tool declarations in YAML are not yet wired
- No equivalent to omniagents' `skills:` block (SKILL.md files referenced by name)
- No `executor:` / `harness:` block — model is a flat field, not a structured executor config
- `$VAR` resolution happens at load time (requires env vars to be set before deploy); omniagents resolves at deploy time on the client side

**Target shape:**
- Wire `tools:` block through to tool registry (currently a no-op)
- Add `skills:` block referencing local SKILL.md files
- Add `executor:` block once Phase 1 harness abstraction lands
- Document the full YAML schema in `docs/AGENT_YAML_SPEC.md`

---

## Phase 4 — Enterprise SaaS tools

### 4.1 Built-in MCP bundles

**Gap:** omniagents ships built-in MCP servers for Glean, Jira, Confluence, Slack,
Google Workspace, and PagerDuty. apx-agent's MCP support is generic (consume any MCP
server) but ships no enterprise bundles.

**Why it matters:** Field engineers deploying agents for enterprise customers need these
tools out of the box. Requiring customers to wire their own MCP servers is friction.

**Priority order (by Databricks FE use frequency):**
1. Slack
2. Google Workspace (Docs, Drive, Gmail, Calendar)
3. Jira
4. Glean
5. Confluence
6. PagerDuty

**Target shape:**
- `apx_agent.mcps.<name>` modules, each a self-contained MCP server subprocess
- Invoked via `mcp_tool("slack", profile="...")` or via YAML `tools.slack.type: mcp_bundle`
- Auth via Databricks-managed OAuth where available, env vars otherwise

---

## Phase 5 — Safety & multi-user

### 5.1 OS-level sandboxing

**Gap:** apx-agent has no filesystem or process isolation. omniagents has `OSEnvSpec` +
`OSEnvSandboxSpec` with Landlock (Linux), bubblewrap, and macOS Seatbelt backends.

**Why it matters:** Agents that can run code or write files in shared Databricks deployments
need isolation — one user's agent should not be able to read another's working directory
or exfiltrate files.

**Target shape:**
- `SandboxConfig` on `AgentConfig` with `read_paths`, `write_paths`, `env_passthrough`
- Landlock backend for Linux (Databricks runtime)
- macOS Seatbelt backend for local dev
- Egress rules DSL (allowlist of outbound hosts/ports)

---

## What apx-agent already leads on (keep and deepen)

These are areas where apx-agent is ahead of omniagents — worth protecting as the above
work proceeds:

- **Semantic memory** (`MemoryStore` with vector recall, Delta + Lakebase backends)
- **Few-shot example store** (`ExampleStore` with `find_similar`)
- **Databricks data-plane tools** (`sql_tool`, `genie_tool`, `uc_function_tool`, `vector_search_tool`)
- **DataAgent / CoworkerAgent** — UC schema introspection, governed SQL, column-grounded instructions
- **Remote agent composition** (`RemoteDatabricksAgent` + A2A card)
- **Template protocol** — declarative agent archetypes extensible via entry points

---

## Summary table

| Capability | apx-agent today | Gap | Phase |
|---|---|---|---|
| Pluggable executor / harness | LangGraph only | High — architectural | 1 |
| Cancellable tools | None | High — multi-user safety | 1 |
| ASK (human-in-the-loop) | None | High — consequential actions | 2 |
| Rich policy system (4 phases, LLM classifier) | Hooks only | Medium | 2 |
| Session labels | None | Medium | 2 |
| Skills (on-demand SKILL.md) | None | Medium — authoring | 3 |
| YAML authoring (tools, skills, executor blocks) | Partial — tools block stripped, no skills/executor | Low-Medium — extend existing path | 3 |
| Built-in enterprise MCP bundles | None | Medium — FE use cases | 4 |
| OS-level sandboxing | None | Lower — shared deployments | 5 |
