# Strands Agent Migration (AWS AgentCore → Databricks)

Moves an existing **Strands Agents SDK** agent off AWS Bedrock AgentCore Runtime and onto Databricks Apps with the
Agent Bricks **`DurableAgentServer`**, without rewriting the agent. Tools and system prompt stay the same.

## What it does

[`before/agent.py`](before/agent.py) is a small order-support agent the way it would run on AgentCore
(`BedrockModel`, `BedrockAgentCoreApp`). [`after/`](after/) is the same agent, deployable with `agentbricks deploy`:
Strands talks to a Databricks Foundation Model API endpoint through its own `OpenAIModel`, conversation history lives
in the managed Session Store, and an interrupted turn is recovered on another instance. The agent module changes in
one place: the module-level `Agent(...)` becomes `build_agent(model, messages)`. See
[`MIGRATION.patch`](MIGRATION.patch) for the full diff.

| AgentCore piece | Replaced by | File |
|---|---|---|
| `BedrockModel` | `strands.models.openai.OpenAIModel` on `<host>/serving-endpoints`, with a Databricks OAuth token | `after/runtime/model.py` |
| `BedrockAgentCoreApp` entrypoint | `DurableAgentServer` with `invoke` / `recover` | `after/runtime/main.py`, `after/runtime/adapter.py` |
| AgentCore Memory / session manager | Strands `messages` saved as Session Store items after each turn | `after/runtime/history.py` |
| AgentCore Observability | `configure_tracing(autolog=mlflow.strands.autolog)` | `after/runtime/main.py` |
| `agentcore launch` | `agentbricks deploy` | `after/agent.toml`, `after/app.yaml` |

Verified on a Databricks workspace with 2 app instances (2026-11-10):

| Capability | Result |
|---|---|
| Model access | FMAPI (`databricks-claude-sonnet-5-5`) as the app's identity, no keys |
| Tool calling | Both `@tool` functions called unchanged |
| Streaming | Text deltas through `context.emit()` |
| Multi-turn history | Turn 2 answered from turn 1; text, `toolUse` and `toolResult` messages stored in the Session Store |
| Crash recovery | Worker killed mid-turn; the other instance ran `recover()` and returned `recovered: true`; history held exactly one turn |
| Tracing | Full Strands traces (model and tool spans) locally and on the recovered run. **Open:** normal deployed requests wrote no trace (see Troubleshooting) |

## Prerequisites

- Databricks workspace with a configured CLI profile
- Access to a Foundation Model API chat endpoint (default `databricks-claude-sonnet-5-5`)
- [`uv`](https://docs.astral.sh/uv/); `uv sync` installs the Agent Bricks CLI (`databricks-agentbricks`)

## Part 1: Workspace setup (one-time)

No one-time workspace setup is required. `agentbricks deploy` creates the Session Store named in `agent.toml` and
grants the app's service principal access to it.

### Step 1 (optional): Bind tracing

```bash
cd after
uv run agentbricks --profile <profile> tracing bind --experiment-name /Shared/agentbricks_traces/strands-support
```

## Part 2: Local development

### Step 1: Install

```bash
cd after
uv sync
```

### Step 2: Run the tests

```bash
uv run --with pytest pytest -q tests   # fake model, no network
```

### Step 3: Run locally

```bash
DATABRICKS_CONFIG_PROFILE=<profile> uv run start-server   # http://localhost:8000
uv run agentbricks endpoint invoke --url http://localhost:8000 --path /api/invocations \
  --json '{"id":"<uuid>","session_id":"<uuid>","input":{"prompt":"Can I still get a refund on order A100?"}}'
```

Without `AGENT_SESSION_STORE`, history is kept in memory.

## Part 3: Deploy to Databricks Apps

### Step 1: Review `app.yaml`

`agentbricks deploy` reads `app.yaml` (not `app.yml`) and rewrites it with the store and package-index settings it
injects. Don't commit the rewritten file. Set `AGENT_ENABLE_CRASH_TEST=1` there only to run the recovery demo.

### Step 2: Deploy

```bash
uv run agentbricks doctor .        # expect 6/8 — see below
uv run agentbricks --profile <profile> deploy strands-support --source . --instances 2
```

`agentbricks doctor` reports **6/8, and that's expected**. Strands isn't an Agent Bricks framework (only
LangGraph and the OpenAI Agents SDK are), so two checks can't honestly pass:

- `agentbricks_dependency` wants the `[openai]` extra, which installs the OpenAI Agents SDK this agent never uses.
- `framework_adapter` wants a `databricks_agentkit.openai.*` call, and the only fitting one,
  `databricks_agentkit.openai.configure_tracing()`, would swap Strands autologging for OpenAI autologging.

Every other check passes, and `deploy` doesn't depend on doctor. Faking the two failures would make the example less
correct, so this example is the documented exception to ADR 0001's "doctor must pass".

### Step 3: Verify

```bash
uv run agentbricks --profile <profile> endpoint invoke agent-bricks-strands-support --path /api/invocations \
  --json '{"id":"<uuid>","session_id":"<uuid>","input":{"prompt":"Is order A200 refundable?"}}'
uv run agentbricks --profile <profile> sessions items list --store strands-support-sessions --session-id <uuid>
```

To see recovery: deploy with `AGENT_ENABLE_CRASH_TEST=1`, send `"crash_test": true` in `input`, then poll
`GET /api/invocations/<id>` until it reports `completed` with `recovered: true`.

Clean up with `uv run agentbricks --profile <profile> deployments delete agent-bricks-strands-support`.

## Configuration

| Variable | Default | What it controls |
|---|---|---|
| `AGENT_MODEL` | `databricks-claude-sonnet-5-5` | FMAPI endpoint Strands calls |
| `AGENT_SESSION_STORE` | unset (in-memory) | Session Store name; injected by `agentbricks deploy` |
| `AGENT_ENABLE_CRASH_TEST` | unset | Demo only: lets `"crash_test": true` kill the worker before history is saved |

## Tools

- `lookup_order(order_id)`: looks up a sample order by ID
- `refund_eligible(days_since_delivery)`: checks the 30-day refund window

## Project structure

```
strands-migration/
├── before/agent.py              # the agent on AgentCore
├── after/
│   ├── support_agent/agent.py   # same tools and prompt; build_agent(model, messages)
│   ├── runtime/
│   │   ├── model.py             # OpenAIModel → FMAPI (+ list-content fix)
│   │   ├── history.py           # Session Store load / append
│   │   ├── adapter.py           # invoke / recover → Strands
│   │   └── main.py              # DurableAgentServer + tracing
│   ├── agent.toml               # agentbricks project (framework label, session store)
│   ├── app.yaml
│   └── tests/test_adapter.py
└── MIGRATION.patch              # diff of the agent module
```

## Troubleshooting

- **`deploy` rejects `framework = "strands"`.** The CLI accepts only `langgraph` or `openai`. `agent.toml` uses
  `openai` as a label; nothing else changes.
- **`doctor` fails `agentbricks_dependency` and `framework_adapter`.** Expected for Strands; see Part 3, Step 2.
  If `project_metadata` fails, commit `.agentbricks/project.toml` and ignore only `.agentbricks/deploy-*`.
- **Intermittent HTTP 500, `TypeError: can only concatenate str (not "list") to str`.** FMAPI can stream
  `delta.content` as a list of parts. `FMAPIModel` in `runtime/model.py` flattens it; keep it in every migration.
- **Tools run twice after a crash.** Strands has no per-step checkpoint, so `recover()` replays the whole turn.
  History is saved only after a turn finishes, so it is never duplicated, but write tools must be idempotent.
- **Traces with a single span.** Don't wrap the turn in an extra MLflow `start_trace` span; Strands autolog already
  opens the root span.
- **`agentbricks tracing list` shows 0 items.** It hides traces whose span data can't be downloaded locally. Check
  with `MlflowClient().search_traces(locations=[<experiment_id>], include_spans=False)`.
- **No traces from normal deployed requests (open).** The same code traces locally and on the deployed recovery
  path; on Apps, the request path recorded no spans. Not yet resolved.
- **`deployment_name` too long.** App names cap at 30 characters, so it can be at most 17.
- **Install fails on Apps.** `uv.lock` must point at public PyPI (`pypi.org/simple`, `files.pythonhosted.org`); the
  repo's `uv-lock-registry` pre-commit hook checks this.
