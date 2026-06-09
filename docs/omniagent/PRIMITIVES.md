# OmniAgents Framework Primitives

## The spec layer — what you author

Everything starts with a YAML bundle. The key file is `config.yaml` (parsed into `AgentSpec`, `omniagents/spec/types.py:1342`):

```yaml
spec_version: 1
name: my-agent

executor:
  harness: claude-sdk          # or codex, openai-agents, databricks
  model: databricks-claude-opus-4-7

tools:
  glean:
    type: mcp
    command: .venv/bin/python
    args: ["-m", "omniagents.inner.databricks_mcps.glean"]

policies:
  my_policy:
    type: function
    handler: mypackage.policy.check
```

The spec is intentionally **self-contained** — all config (API keys via `${ENV_VAR}`, tool parameters, feature flags) is resolved at deploy time on the client, never at runtime on the server.

---

## Agent-definition primitives (inner stack)

**`AgentDef`** (`inner/datamodel.py:605`) — The in-memory representation after parsing. Holds the system prompt, tool registry, executor, policies, memory scopes, sandbox config, and skill registry.

**Tool hierarchy** (`inner/tools.py`):

| Type | Purpose |
|---|---|
| `FunctionTool` | Python callable or UC function (`catalog.schema.func`) |
| `CancellableFunctionTool` | Long-running async work that can be cancelled mid-flight |
| `MCPTool` | Remote MCP server tool (HTTP/SSE or stdio) |
| `AgentTool` | Sub-agent invocation — a full `AgentDef` treated as a tool |
| `SkillTool` | Loads a `SKILL.md` file into context on demand |
| `HandoffTool` | Transfers the conversation connection to another agent |

**`Message` / `History` / `Connection`** (`inner/datamodel.py:50–184`) — The three conversation primitives. `Connection` is the bidirectional channel between user and agent; `History` is the message log with a context-window view; `Message` carries role, content, and metadata.

---

## Policies — the governance primitive

Policies (`inner/policies.py`) intercept at four phases: `request`, `response`, `tool_call`, `tool_result`. Each returns `ALLOW / ASK / DENY` with an optional reason. `ASK` pauses execution and waits for human approval.

```python
@dataclass
class PolicyResult:
    action: PolicyAction   # ALLOW | ASK | DENY
    reason: str | None
    set_labels: dict[str, str]   # can attach labels to the session
```

Multiple policies compose with max-action semantics (DENY beats ASK beats ALLOW).

---

## Executor — the LLM harness abstraction

`Executor` (`inner/executor.py`) is the interface that translates an agent turn into LLM calls and tool dispatch. It emits a stream of typed events:

- `TextChunk` — streaming delta
- `ReasoningChunk` — thinking/reasoning blocks
- `ToolCallRequest` — LLM wants to call a tool
- `ToolCallComplete` — tool result came back
- `TurnComplete` — final response + token usage

Current harnesses: `claude-sdk`, `codex`, `openai-agents`, `databricks` (routes through the Databricks gateway), `openai-responses`.

---

## Sandbox & OS environment

`OSEnvSpec` + `OSEnvSandboxSpec` (`inner/datamodel.py:361–559`) are first-class config for filesystem isolation: which paths the agent can read/write, whether network is allowed, egress rules (MITM proxy DSL), env var allowlists, and tmux terminal sessions. Backends: Linux Landlock, bubblewrap (bwrap), macOS Seatbelt.

This is the primitive that makes the framework Databricks-oriented — agents running in shared deployments get declarative, spec-driven sandboxing rather than ad-hoc process restrictions.

---

## Databricks-specific built-ins

The framework ships built-in MCP servers under `inner/databricks_mcps/` for Glean, Jira, Confluence, Slack, Google Workspace, and PagerDuty — consumed like any other MCP tool in the YAML. There's also a `DatabricksExecutor` that routes LLM calls through the Databricks model serving gateway using CLI profiles.

---

## Stores layer (server-side runtime)

`ConversationStore`, `ArtifactStore`, `AgentStore`, `FileStore` (`stores/`) are abstract Protocol-style interfaces for durable execution state. These are what the server uses to persist conversation history, agent bundles (as tarballs), and file artifacts across sessions. The inner stack doesn't touch these — they're purely the distributed runtime's concern.

---

## Key design principle

The framework's core bet is the **spec as the unit of deployment**: you author YAML + Python tools, the spec is bundled and shipped, and the runtime executes it against whichever harness and infrastructure is available — with policies and sandboxing as first-class spec-level config rather than server-side config.
