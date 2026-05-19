# shortage-intelligence — migration to the apx-agent 2026-05 primitives

A dry-run analysis of what would change if we re-wrote this example today against the current apx-agent surface. Most of the agent code stays put — the tools are inherently user-scoped — but the *deploy* and *operate* sides pick up substantial new affordances.

## The agent today

5-step `SequentialAgent` (`pipeline.py`) with 5 plain-Python tool functions (`agent_router.py`). Each tool takes `Dependencies.Workspace` for OBO and either:

| Tool | What it does | Auth scope |
|---|---|---|
| `scan_demand_clusters` | `run_sql` against the demand-orders Delta table OR Genie space | user-scoped UC |
| `find_historical_patterns` | `run_sql` against the historical-demand Delta table | user-scoped UC |
| `validate_against_market_news` | Vector Search query + Tavily news API | user-scoped UC + external HTTP |
| `check_vendor_availability` | DigiKey API (external OAuth) | external HTTP |
| `find_alternative_parts` | Vector Search query | user-scoped UC |

## What stays put

**Every existing tool stays Python-only.** All five touch either user-scoped Databricks APIs (SQL, Vector Search, Genie) or external HTTP services that need to honor the calling user's identity. UC Python functions execute server-side under the function owner's identity — the user's grants don't pass through. Forcing these into UC functions would break the governance story; keep them as `Dependencies.Workspace`-injecting tools.

The `SequentialAgent` orchestration is the right shape. Each step's output threads through the conversation history; the synthesis step sees everything. No reason to switch to `HandoffAgent` (the routing is fixed) or `RouterAgent` (no branching).

## What changes

### Deploy path — `log_agent` + UC tags

Today: no deploy script. The agent runs as a Databricks App via the FastAPI wrapping in `app.py`.

Recommended: add `deploy.py` that uses the canonical primitives:

```python
import mlflow
from databricks import agents
from apx_agent import log_agent, set_uc_tags_for_agent
from shortage_intelligence_agent.backend.agent_router import agent

REGISTERED_NAME = "main.agents.shortage_intelligence"
MODEL_ENDPOINT = "databricks-claude-sonnet-4-6"

with mlflow.start_run():
    info = log_agent(
        agent,
        model=MODEL_ENDPOINT,
        registered_model_name=REGISTERED_NAME,
        experiment="/Users/me@company.com/agents/shortage_intelligence",
    )

agents.deploy(REGISTERED_NAME, model_version=info.registered_model_version)
set_uc_tags_for_agent(
    agent,
    registered_model_name=REGISTERED_NAME,
    model=MODEL_ENDPOINT,
    name="shortage_intelligence",
)
```

This unlocks:
- `apx list` finds the agent via `apx.agent.name` UC tag
- `apx info / apx test / apx trace / apx logs / apx cost` all work against it
- `apx topology` renders the (single-node) graph
- MLflow traces emit the standard `apx.*` audit attrs — `apx.agent.name`, `apx.model.endpoint`, `apx.tool.name`, `apx.session.id` — so watchdog (or any downstream consumer) can correlate runtime activity without parsing agent-specific schemas

### Pure-logic helpers — extract as `@tool(uc=...)`

Some logic in the current tools is *post-processing* on data already fetched user-side. That logic can move into UC functions, callable by Genie and Managed MCP independent of this agent:

| Candidate | Source | Why it's a UC function candidate |
|---|---|---|
| `classify_shortage_severity(price_delta_pct, customer_count) -> str` | derived from `find_historical_patterns` output | Pure math + thresholds. Useful for any analyst querying historical patterns in Genie. |
| `format_sourcing_action(component_id, qty, vendor) -> str` | derived from report-generation step | Pure string formatting. Data team might want it in dashboards. |
| `next_business_day(date, region="US") -> date` | (if it existed) | Date arithmetic — pure, broadly reusable. |

The first is implemented in this round (`classify_shortage_severity`) as a worked example.

The other agent-internal helpers (`_scan_via_genie`, `_get_digikey_token`) stay private — they wrap stateful external clients, not pure logic.

### Sessions

Today this agent is one-shot per request (the SequentialAgent runs the full 5 steps each call). For an interactive UX where the sourcing team asks follow-ups, wire `DeltaSessionStore` at deploy time:

```python
from apx_agent import compile_to_chat_agent, DeltaSessionStore

store = DeltaSessionStore(
    table_path="main.agents.shortage_intelligence_sessions",
    ws=ws,
    warehouse_id=os.environ["DATABRICKS_WAREHOUSE_ID"],
)
chat_agent = compile_to_chat_agent(agent, model="...", session_store=store)
```

Then callers thread `custom_inputs={"session_id": "<thread-id>"}` to continue conversations.

### Watchdog + local guards

Today: no governance layer. Recommended:

```python
from apx_agent import (
    WatchdogClient, WatchdogGuard, make_watchdog_transport,
    compose, RateLimit, prompt_injection_heuristic,
)

watchdog = WatchdogClient(transport=make_watchdog_transport(
    mcp_url=os.environ["WATCHDOG_MCP_URL"],
    mcp_tool_name="evaluate_operation",
    violations_table="main.watchdog.runtime_violations",
    ws=ws,
))
guard = WatchdogGuard(watchdog, agent_name="shortage_intelligence")

# In pipeline.py, wire into each step's Agent:
detection_agent = Agent(
    tools=[scan_demand_clusters],
    instructions="...",
    input_guardrails=[prompt_injection_heuristic(), guard.for_input()],
    before_tool=compose(
        RateLimit(per_minute=120),  # DigiKey rate limit lives upstream of this
        guard.for_tool(),
    ),
)
```

Layered: zero-latency local checks (rate limit, regex injection) first, watchdog full posture eval second.

### Cross-agent eval

Today: no eval scaffolding. With `evalset.jsonl` + `evaluate_chain`:

```bash
apx eval-chain evalset.jsonl \
    --module shortage_intelligence_agent.backend.agent_router:agent \
    --model databricks-claude-sonnet-4-6 \
    --experiment /Users/me@company.com/agents/shortage_intelligence
```

The 5-step `SequentialAgent` means each prompt produces 5 child spans — `evaluate_chain` correlates them via `apx.tool.name` + `apx.subagent.endpoint` to report which steps fired per prompt. Useful for catching when a step gets accidentally skipped (e.g. early-exit when the demand scanner finds nothing).

## What's blocked

- **No tool migrates to a UC function** — every existing tool needs OBO. The `@tool(uc=...)` pattern is for pure-logic helpers; this agent's tools are I/O.
- **No sub-agent / supervisor split today** — the `SequentialAgent` is one agent; `apx topology` shows one node. Splitting steps into separately-deployed endpoints (so e.g. the vendor team owns `vendor_agent`) is a bigger refactor and not currently warranted by the workload.

## Effort estimate

| Move | Effort | Status this round |
|---|---|---|
| `deploy.py` script using the canonical primitives | XS — ~40 lines | ✅ shipped |
| `evalset.jsonl` covering the 5 steps | XS — ~20 lines | ✅ shipped |
| Extract `classify_shortage_severity` as `@tool(uc=...)` | XS — ~30 lines | ✅ shipped |
| Wire `DeltaSessionStore` at deploy time | S — ~20 lines | Documented, not implemented (depends on UX decision) |
| Wire `WatchdogGuard` + local guards into pipeline.py | S — ~30 lines | Documented, not implemented (depends on watchdog availability) |
| Split into 5 separately-deployed sub-agents | M — bigger refactor | Not warranted yet |
