# Tracing

Every apx-agent run is traced automatically. No configuration required — when MLflow is installed, spans are emitted for every agent invocation, tool call, and model call. Traces flow into the dev UI during local development and into Databricks Managed MLflow when deployed.

> **Coming from OpenAI Agents SDK or ADK?**
> This is equivalent to OpenAI Agents SDK's tracing (OpenAI backend → MLflow) and ADK's built-in Cloud Trace integration. Same mental model: automatic, per-request, structured.

## How tracing works

apx-agent emits MLflow spans at three levels for every request:

| Span type | What it covers |
|---|---|
| `AGENT` | One top-level `ChatAgent.predict` / `predict_stream` call |
| `TOOL` | Each tool dispatch — `@tool` functions, `sql_tool`, `genie_tool`, UC functions |
| `LLM` | Each model call, including token counts |
| `CHAIN` | Sub-agent dispatch (remote `/invocations` calls) |

Tracing is **always on** when MLflow is installed. It never adds overhead that blocks a request — if the tracing backend is unreachable, the agent continues running and the span is silently dropped.

## Dev UI — `/_apx/traces`

During local development (`apx run`), a trace browser is available at `/_apx/traces`:

```
http://localhost:8000/_apx/traces
```

It lists every trace from the current session, newest first. Click any row to see the full span tree: inputs, outputs, `apx.*` attributes, token counts, and latency per span. This is the fastest way to answer "what did my agent actually do?"

Deep-link to a specific trace: `/_apx/traces/{trace_id}`.

## MLflow experiments

When deployed to Databricks, traces flow into MLflow. Each agent writes to the experiment configured in `pyproject.toml`:

```toml
[tool.apx.agent]
experiment = "/Users/me@company.com/agents/customer_triage"
```

View traces in the Databricks UI: open the experiment → **Traces** tab. Each row is one request. The span waterfall shows the full agent loop: tool calls, model calls, sub-agent dispatches.

If no experiment is set, MLflow uses its currently-active experiment. See [Evaluation](../evaluate/overview.md) for how experiments and eval results share the same workspace path.

## Deep LangGraph tracing

apx-agent emits a focused span set by default (agent, tools, model calls). `apx run` automatically enables full LangGraph-level spans for the dev loop — each graph node, edge, and conditional branch appears in `/_apx/traces` without any configuration.

Production deploys leave deep tracing off. The reason: `mlflow.langchain.autolog()` logs the full LangGraph model as an MLflow artifact, not just traces — that serialization adds ~30s to the first request. The focused span set captures everything useful for the trace browser without that cost.

To force deep tracing on in production, or off in dev:

```bash
APX_AGENT_MLFLOW_AUTOLOG=1 apx run   # already the default in dev — explicit override
APX_AGENT_MLFLOW_AUTOLOG=0 apx run   # force off, even in dev
```

To remove tracing entirely, don't install the `eval` extra — tracing is a no-op when MLflow isn't installed:

```bash
pip install 'apx-agent'       # no tracing
pip install 'apx-agent[eval]' # with tracing
```

## The `apx.*` span attribute schema

Every framework-emitted span carries a stable set of `apx.*` attributes. These are set by the framework — you don't need to add them manually. They're defined in `AuditAttrs`:

### Agent & session
| Attribute | Value |
|---|---|
| `apx.agent.name` | Agent name from `[tool.apx.agent]` |
| `apx.agent.version` | Deployed version |
| `apx.session.id` | Session identifier for multi-turn conversations |
| `apx.operation` | `predict` \| `predict_stream` \| `tool_call` \| `model_call` \| `sub_agent_call` |

### User identity (no PII)
| Attribute | Value |
|---|---|
| `apx.user.token_provided` | `true` if an OBO token was passed |
| `apx.user.hash` | SHA-256(user_id) — for correlation without logging raw IDs |

### Tool calls
| Attribute | Value |
|---|---|
| `apx.tool.name` | Tool function name |
| `apx.tool.uc_function` | Three-part UC name when the tool wraps a UC function |
| `apx.tool.input_keys` | Comma-separated argument names (shape, not values) |
| `apx.tool.input_hash` | SHA-256 fingerprint of the input (not the input itself) |
| `apx.tool.output_type` | Python type name of the return value |
| `apx.tool.output_size` | `len()` of the return value |
| `apx.tool.duration_ms` | Wall-clock time for the tool call |

### Model calls
| Attribute | Value |
|---|---|
| `apx.model.endpoint` | Model serving endpoint used |
| `apx.model.input_tokens` | Prompt token count |
| `apx.model.output_tokens` | Completion token count |
| `apx.model.streaming` | `true` for streaming requests |

### Sub-agents
| Attribute | Value |
|---|---|
| `apx.subagent.endpoint` | Remote `/invocations` URL |
| `apx.subagent.name` | Display name of the sub-agent |

### Watchdog decisions
| Attribute | Value |
|---|---|
| `apx.watchdog.action` | `allow` \| `reject` \| `redact` |
| `apx.watchdog.policy_id` | Which guard fired |
| `apx.watchdog.reason` | Human-readable reason |

These attributes flow into `system.access.audit_logs` and workspace-level trace tables on Databricks, so compliance queries don't need agent-specific schemas.

## Exporting traces to Delta

For analytics — cost rollups, tool-call frequency, latency P95, prompt heatmaps — export traces to a Delta table:

```bash
apx export-traces \
  --experiment "/Users/me@company.com/agents/customer_triage" \
  --table main.analytics.agent_traces \
  --hours 24
```

Or from Python:

```python
from databricks.sdk import WorkspaceClient
from apx_agent import export_traces

result = export_traces(
    experiment_name="/Users/me@company.com/agents/customer_triage",
    target_table="main.analytics.agent_traces",
    ws=WorkspaceClient(),
    lookback_hours=24,
)
print(f"Wrote {result.rows_written} traces to {result.target_table}")
```

The exporter auto-creates the table on first run and uses a MERGE upsert, so it's safe to run on a schedule.

### Delta table schema

```sql
CREATE TABLE agent_traces (
    trace_id             STRING NOT NULL,
    experiment_id        STRING,
    agent_name           STRING,
    operation            STRING,    -- predict | tool_call | model_call | ...
    status               STRING,
    start_time_ms        BIGINT,
    execution_time_ms    BIGINT,
    session_id           STRING,
    user_token_provided  BOOLEAN,
    model_endpoint       STRING,
    tool_count           INT,
    watchdog_action      STRING,    -- allow | reject | redact | NULL
    watchdog_policy_id   STRING,
    tags                 STRING,    -- JSON of remaining apx.* attrs
    exported_at          TIMESTAMP
) USING DELTA
```

### Example queries

**Cost by agent per day** (join to `system.billing.usage`):

```sql
SELECT
  t.agent_name,
  date_trunc('day', from_unixtime(t.start_time_ms / 1000)) AS day,
  count(*) AS requests,
  avg(t.execution_time_ms) AS avg_latency_ms
FROM main.analytics.agent_traces t
GROUP BY 1, 2
ORDER BY day DESC, requests DESC
```

**Tool call frequency**:

```sql
SELECT
  get_json_object(tags, '$.apx\\.tool\\.name') AS tool_name,
  count(*) AS calls,
  avg(execution_time_ms) AS avg_ms
FROM main.analytics.agent_traces
WHERE operation = 'tool_call'
GROUP BY 1
ORDER BY calls DESC
```

**Watchdog rejections**:

```sql
SELECT
  watchdog_policy_id,
  watchdog_action,
  count(*) AS events
FROM main.analytics.agent_traces
WHERE watchdog_action IN ('reject', 'redact')
GROUP BY 1, 2
ORDER BY events DESC
```

## Adding custom span attributes

For application-level attributes beyond the built-in `apx.*` schema, use `safe_span` and `set_span_attribute` from the internal tracing module. These are not part of the public API surface but are available for advanced use:

```python
from apx_agent._mlflow_tracing import safe_span, set_span_attribute

@tool
def classify_intent(query: str) -> str:
    """Classify the intent of a user query."""
    with safe_span("classify_intent", span_type="TOOL") as span:
        result = _run_classifier(query)
        set_span_attribute(span, "app.intent.category", result.category)
        set_span_attribute(span, "app.intent.confidence", result.confidence)
        return result.label
```

For audit-relevant attributes that need to appear consistently across agents, open a PR to add them to `AuditAttrs` in `_audit.py` — the constants enforce naming discipline across the codebase.

## Related

- [Dev UI](../get-started/dev-ui.md) — `/_apx/traces` and other dev surfaces
- [Evaluate](../evaluate/overview.md) — MLflow experiments and eval results
- [Guardrails & Safety](../safety/callbacks.md) — watchdog decisions in traces
- [Cost tracking](../reference/cost-tracking.md) — `cost_for_agent`, CLI cost surface
