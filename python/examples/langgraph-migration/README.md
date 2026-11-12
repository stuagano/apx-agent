# LangGraph Agent Migration (existing graph → Databricks)

Move an **existing, hand-written LangGraph agent** onto Databricks Apps with Agent Bricks
`DurableAgentServer`, without rewriting the graph. The graph logic is unchanged
([`MIGRATION.patch`](MIGRATION.patch) is the whole diff: a 2-line crash-test hook). The new code is
about 100 lines connecting the graph to the runtime.

## What it does

`before/` is a typical self-hosted agent: a LangGraph root-cause-analysis (RCA) graph with custom
state (`tenant`, `kpis`, `anomalies`) behind a FastAPI `POST /rca` endpoint, using an in-memory
checkpointer. The graph computes a tenant's KPIs, flags any KPI below its threshold, and asks an LLM
for a root-cause hypothesis.

`after/` is the same graph on `DurableAgentServer`:

| Gained | How |
|---|---|
| Sync, streaming, and background calls with polling and event replay | `DurableAgentServer` invocation API (`/api/invocations`) |
| Graph checkpoints that survive restarts | `checkpointer()` → the Session Store declared in `agent.toml` |
| **Crash recovery that resumes from the last checkpoint** | `recover()` resumes when the latest checkpoint belongs to this invocation, else replays the input |
| Tracing | `configure_tracing()` + `start_trace()` |

The migration was produced with `agentbricks init --existing`, which generates a migration skill for
a coding agent and leaves your code alone.

**Verified:** deployed with 2 replicas (agentbricks 0.7). A worker killed mid-run recovered on the
other replica and re-ran only the unfinished node:

```
run.started → step compute_kpis → step detect → [worker killed]
run.started → step analyze → run.completed          (attempt 2, other replica)
```

Re-verified on agentbricks 0.9.0: `agentbricks doctor` 8/8, offline tests, and a local run (sync and
streaming).

## Prerequisites

- `uv`, Python ≥ 3.11
- A Databricks workspace with Apps, and a chat model serving endpoint (`MODEL` in `rca/graph.py`)
- The Databricks CLI, logged in to that workspace

## Part 1: Local development

### Step 1: Install

```bash
cd after
uv sync
```

### Step 2: Run the tests

```bash
uv run --with pytest pytest -q tests     # offline: fake model, in-memory checkpointer
```

They prove the recovery decision: after a crash before `analyze`, `recover()` resumes from the
checkpoint and re-runs only `analyze` (the KPI and detect steps are not replayed).

### Step 3: Run locally

```bash
DATABRICKS_CONFIG_PROFILE=<profile> uv run start-server      # http://localhost:8000
curl -s localhost:8000/api/invocations -H 'content-type: application/json' \
  -d '{"id":"'$(uuidgen)'","session_id":"t-1","input":{"tenant":"acme"}}'
```

## Part 2: Deploy to Databricks Apps

### Step 1: Review `app.yaml`

`agentbricks deploy` rewrites `app.yaml` with the store and index settings it injects. Commit only
the command and the commented demo switch.

### Step 2: Deploy

```bash
uv run agentbricks doctor .                                   # expect 8/8
uv run agentbricks --profile <profile> deploy --instances 2 --no-wait
uv run agentbricks --profile <profile> deployments wait agent-bricks-langgraph-rca
```

### Step 3: Verify

```bash
inv() { uv run agentbricks --profile <profile> endpoint invoke agent-bricks-langgraph-rca "$@"; }
inv --path /api/invocations --json '{"id":"'$(uuidgen)'","session_id":"t-1","input":{"tenant":"acme"}}'

ID=$(uuidgen)   # background, then poll and replay events
inv --path /api/invocations --json '{"id":"'$ID'","session_id":"t-2","input":{"tenant":"globex"},"background":true,"stream":true}'
inv --method GET --path /api/invocations/$ID
inv --method GET --path /api/invocations/$ID/events --query after=0 --sse
```

**Crash recovery** (demo only): uncomment `AGENT_ENABLE_CRASH_TEST` in `app.yaml` and redeploy, then send
`"crash_test": true` with `"background": true`. Poll with `--routing-key $(uuidgen)` until you reach the
surviving replica. The output carries `"recovered": true, "resumed_from_checkpoint": true`. The killed
replica stays down until the next deploy; see
[runtime targets](../../../docs/running/runtime-targets.md). Redeploy before demoing again.

## Request contract

| | Before | After |
|---|---|---|
| Endpoint | `POST /rca` | `POST /api/invocations` |
| Body | `{tenant, thread_id, question}` | `{id, session_id, input: {tenant, question}}`. `id` is an idempotency key; `session_id` replaces `thread_id` |
| Response | `{anomalies, report}` | `{id, status, output: {anomalies, report}}` |
| Long runs | caller holds the connection | `"background": true` → `202` + `status_url`; add `"stream": true` for `events_url` |

Send a stable `X-Routing-Key` per session so a session stays on one replica.

## Decisions a real migration has to make

- **Tenant identity.** This example takes `tenant` from the request body, so the caller asserts it. Derive
  it from the verified caller instead. One option is a service principal per tenant, reached through OAuth
  token federation from your own identity provider; the app then acts as that tenant. See
  [identity passthrough](../../../docs/safety/identity-passthrough.md).
- **Side effects.** Recovery is at-least-once. Make writes and notifications idempotent (e.g. `MERGE` on the run ID).
- **History.** Switching to the Session Store starts threads fresh; copying old checkpoints is a separate job.
- **Request-user runs** (on-behalf-of a user) can't be recovered or paused for approval. Service-principal runs can.

## Project structure

```
langgraph-migration/
├── MIGRATION.patch            # before → after graph diff (2 lines)
├── before/rca/
│   ├── graph.py               # the original LangGraph graph
│   └── server.py              # the original FastAPI /rca endpoint (removed in after/)
└── after/
    ├── agent.toml             # framework, server, Session Store binding
    ├── app.yaml               # start command + commented demo switch
    ├── .agentbricks/project.toml
    ├── rca/graph.py           # unchanged graph + 2-line demo hook
    ├── runtime/main.py        # DurableAgentServer entrypoint
    ├── runtime/adapter.py     # request/event translation + recovery decision
    └── tests/test_recovery.py # offline checkpoint-resume tests
```

## The apx version

`after/` uses Agent Bricks directly, with no apx code. [`after-apx/`](after-apx/) is the same, unchanged graph
(`after-apx/rca/graph.py` is byte-identical to `before/`) as an apx `GraphAgent`. All the wiring is in
`agent.py`, and apx adds governed tools, recovery, approvals and A2A on top. To get there from your own project:

```bash
apx-agent agents migrate-langgraph .          # writes apx-migrate/SKILL.md for your coding agent
apx-agent agents migrate-langgraph . --check  # verifies it: compile, entrypoint, both doctors
```

See [GraphAgent](../../../docs/agents/graph-agent.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `doctor` fails `project_metadata` | Commit `.agentbricks/project.toml`; ignore only `.agentbricks/deploy-*` |
| `401` calling the app as a service principal | Grant it `CAN USE` on the app (the proxy returns 401, not 403) |
| Requests to one replica return `502` after a crash | That replica stays down until redeploy; recovery still completes on the others |
| Deploy can't install packages | `uv.lock` must resolve from public PyPI (`pypi.org`, `files.pythonhosted.org`) |
