# apx-agent

[![CI](https://github.com/stuagano/apx-agent/actions/workflows/test.yml/badge.svg)](https://github.com/stuagano/apx-agent/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Build governed Databricks agents. Write a Python object — apx-agent compiles it to whichever Databricks runtime you target.

## LlmAgent — you control the loop

`LlmAgent` (aliased as `Agent`) is an LLM + tools + a loop. You decide what it can call, when it stops, and what happens before and after each step.

```python
from apx_agent import LlmAgent, uc_function_tool, genie_tool

agent = LlmAgent(
    instructions="Investigate customer accounts.",
    tools=[
        uc_function_tool("main.tools.lookup_account"),
        genie_tool("abc123", description="Answer billing questions"),
    ],
    max_iterations=10,
    # memory="persistent",   # durable semantic recall across sessions
)
```

Every hook is optional. None requires subclassing.

```python
from apx_agent import run_once

# Invoke the agent (no HTTP request needed)
result = run_once(agent, "Look up account 42.")
print(result)
```

**Compose loops explicitly.** `LoopAgent` iterates until a condition is met; `SequentialAgent` pipelines agents in order; `ParallelAgent` fans out; `HandoffAgent` routes conversationally.

```python
from apx_agent import SequentialAgent

investigation = SequentialAgent(
    agents=[presence_check, lineage_trace, code_analysis, synthesis],
    instructions="Investigate why data is missing.",
)
```

See [docs/agents/composition.md](docs/agents/composition.md) for the full composition reference.

Three agent types cover most use cases:

| | |
|---|---|
| **`LlmAgent`** | The base. You own the loop: tools, hooks, guardrails, iteration cap. |
| **`DataAgent`** | One line over a Unity Catalog schema. Grounded in real columns, runs as the calling user. |
| **`CoworkerAgent`** | Joins two source systems on a shared key. Persona, join key, objective. |

Deploy to Databricks Apps or Mosaic AI Model Serving — same agent definition, one flag changes the target.

```bash
uv add apx-agent
uv run apx-agent doctor                          # check auth & environment first
uv run apx-agent agents scaffold my-agent
cd my-agent && uv sync
uv run apx-agent agents deploy --target apps
```

`doctor` verifies your Databricks auth, tooling, and config before you scaffold. `scaffold` writes an editable `my-agent/` project directory containing the agent code and Apps bundle files. Run `deploy` from that directory; it bundles the project and prints the App URL when done. You can also deploy a hand-authored YAML spec by passing its path to `deploy`.

---

## What is apx-agent?

Building agents on Databricks means dealing with a stack of systems that all speak different languages: LLM APIs have incompatible wire formats, memory backends have different interfaces, conversation history looks different depending on the framework, and trace schemas differ by SDK. Wiring all of that together correctly — and keeping it working as the stack evolves — is the problem nobody wants to have.

**apx-agent is the normalization layer.** You declare what your agent should be. apx-agent makes it work and makes it observable, regardless of what's underneath.

```toml
[tool.apx.agent]
name = "payroll-coworker"
model = "databricks/claude-3-7-sonnet"
instructions = "You are a payroll analyst..."

[tool.apx.agent.memory]
type = "lakebase"
host = "${LAKEBASE_HOST}"
database = "payroll"
table_name = "main.payroll.agent_memory"
embedding_model = "databricks-bge-large-en"
embedding_dim = 1024

[tool.apx.agent.data]
catalog = "main"
schema = "payroll"
```

That declaration becomes: an agent grounded in its schema before the first question, durable memory that persists across sessions, a dev UI that surfaces tool calls and conversation correctly regardless of which underlying API format produced them, and a deployment target that enforces Unity Catalog grants per-caller without any per-agent configuration.

**What gets normalized so you don't have to think about it:**

| Layer | What apx-agent hides |
|---|---|
| **LLM API format** | Responses API and chat-completions traces both surface identically in the dev UI |
| **Conversation history** | One canonical message format across all agent types and frameworks |
| **Memory backends** | Lakebase, UC managed memory, or in-memory — same interface, declared not implemented |
| **Observation** | Tool calls, spans, and conversation deltas normalized before they reach any renderer |
| **Governance** | Identity passthrough, UC grants, and audit logging wired from the declaration |
| **Multi-agent** | `sub_agents=[url]` + A2A — agents call each other across apps, identity passed through per hop, all declared |

You write a Python object or a TOML block. The normalization work is apx-agent's job.

### The same agent, by hand vs. declared

A typical "build a support agent on Databricks" notebook — ground it in Vector Search, wire two tools, run an agentic loop, trace it, log a served model — is about **220 lines** across the setup, the hand-authored tool schemas, the tool-calling loop, and a second copy of the tools-and-loop re-implemented inside a `PythonModel` for serving. apx-agent collapses that to a declaration plus the tool factories:

| Step | By hand (raw SDK notebook) | apx-agent |
|---|---|---|
| **Ground** | `query_index(...)` call + manual row unpacking | `vector_search_tool(index, columns=..., num_results=...)` |
| **Tools** | Two functions **+ hand-written OpenAI JSON schemas** | `vector_search_tool(...)`, `uc_function_tool(...)` — schemas introspected |
| **Loop** | Hand-rolled `run_agent` — `max_turns`, `tool_call_id` bookkeeping, `model_dump(exclude_none=True)` | runtime-owned; you set `max_iterations` |
| **Trace** | `@mlflow.trace` + `with mlflow.start_run(...)` wrappers | automatic |
| **Ship** | ~90 lines: tools **and loop re-implemented** inside a `PythonModel`, temp `.py`, `infer_signature`, pinned `pip_requirements` | `apx-agent agents deploy --target apps` (or `serving`) |
| **Govern** | tools run as the *notebook user* (`spark.table`) | tools run under the **calling user's** UC grants (OBO) |

Net: **~220 lines → ~15 lines + a TOML block (~90% less code)** — and the deleted parts are the drift-prone ones. The raw notebook maintains the loop and both tools *twice* (once to demo, once inside the logged model); apx-agent serves the same object you ran locally. The one thing that doesn't shrink is the eval golden-set — that's real domain work, not boilerplate. See [docs/positioning.md](docs/positioning.md#by-hand-vs-declared-a-worked-comparison) for the full worked example.

---

## Quickstart

Python 3.11+ required.

**1. Install**

```bash
uv add apx-agent
```

**2. Scaffold an agent project**

```bash
uv run apx-agent agents scaffold my-agent
```

The scaffold writes an editable `my-agent/` project in the current directory. It includes `agent.py`, `pyproject.toml`, `databricks.yml`, the generated Apps server, and the baked schema manifest when schema discovery succeeds.

**3. Deploy the project**

```bash
cd my-agent && uv sync
uv run apx-agent agents deploy --target apps
```

`deploy` bundles the current project and creates a Databricks App. It prints the URL when done. A hand-authored YAML spec can still be deployed by passing its path to `agents deploy`.

**4. Run locally**

```bash
uv run apx-agent agents run --reload
```

FastAPI starts on `:8000`; chat at `/_apx/agent`, view traces at `/_apx/traces`, author new tools via the **New Tool** modal and inspect live tool schemas in the right panel of the Edit page (`/_apx/edit`) — the standalone `/_apx/tools` page is retired and redirects there. `agent.py` edits are picked up on restart — pass `--reload` (off by default) for auto-reload during local dev.

> **Something not working?** Run `uv run apx-agent doctor` — checks Python, uv, Databricks CLI, auth, and project layout. Prints a `Fix:` line for anything wrong.

See [docs/get-started/quickstart.md](docs/get-started/quickstart.md) for the full walkthrough.

### Know what you're pointed at

`apx-agent status` prints the active Databricks profile and project/target — offline, no API call — so you can confirm context before you deploy:

```bash
$ apx-agent status
profile: fe-stable
project: payroll-coworker
target:  apps
```

`--prompt` emits a compact one-liner (`apx:payroll-coworker(apps) ▸ fe-stable`). It's safe in an async/cached prompt segment (e.g. starship `[custom]`, powerlevel10k async), but the CLI cold-starts in ~1s, so don't call it on every render of a synchronous `PS1`. For an instant, zero-overhead prompt the same facts read straight from the shell:

```bash
apx_ps1() {
  local p="${DATABRICKS_CONFIG_PROFILE:-DEFAULT}"
  [ -f pyproject.toml ] && grep -q '\[tool.apx.agent\]' pyproject.toml && printf 'apx ▸ %s ' "$p"
}
setopt PROMPT_SUBST 2>/dev/null; PROMPT='$(apx_ps1)'"$PROMPT"
```

---

## DataAgent — one line over a UC schema

```python
from apx_agent import DataAgent

agent = DataAgent("main", "sales")
```

That's a working agent. It knows the tables and columns in `main.sales` before the first question — no `SHOW TABLES` at runtime, no discovery prompt, no hallucinated schema.

Schema discovery priority (first match wins):

1. **Baked schema** — `.apx/schema.json`, written from the UC Tables API when the project is generated by `apx-agent agents scaffold`, or at `apx-agent agents deploy my-agent.yaml` time for a hand-authored spec. Ships with your code.
2. **Live introspection** — pass `ws=WorkspaceClient()` for fresh schema at construction time.
3. **Explicit override** — pass `tables={"orders": ["id(bigint)", ...]}` for tests.
4. **Ungrounded fallback** — discovers schema with SQL on the first turn.

```python
# Live introspection
from databricks.sdk import WorkspaceClient
agent = DataAgent("main", "sales", ws=WorkspaceClient())

# Add persona, Genie space, vector search, or UC functions
agent = DataAgent(
    "main", "sales",
    persona="a revenue analyst",
    genie_space="abc123",
    vector_index="main.sales.product_docs",
    extra_tools=[uc_function_tool("main.tools.send_alert")],
)
```

**Governance:** deploy once, everyone runs as themselves. The app forwards each caller's OAuth token per request, and Unity Catalog enforces their grants on their data. See [docs/safety/identity-passthrough.md](docs/safety/identity-passthrough.md).

See [docs/agents/data-agent.md](docs/agents/data-agent.md) for the full reference.

---

## CoworkerAgent — join two source systems

Two source systems landed in a UC schema. One business entity links them. One question neither system can answer alone.

```python
from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
    join_key="employee ID",
    objective="surface mismatches between hours worked and paychecks issued",
    # memory="persistent",  # remember facts across sessions
)
```

The `join_key` and `objective` are woven into the agent's grounded instructions. Common patterns:

| Use case | System A | System B | Join key |
|---|---|---|---|
| Payroll reconciliation | Kronos (hours worked) | Workday (paychecks) | employee ID |
| Quote-to-cash | Salesforce (deals) | NetSuite (invoices) | opportunity ID |
| Onboarding / offboarding | Workday (employment) | Okta (access) | employee ID |
| Warranty & entitlement | ServiceNow (cases) | SAP (contracts) | asset serial number |
| Order status | Oracle ERP (orders) | TMS (freight) | PO / shipment number |
| Claims integrity | Epic (chart) | Claims system (coding) | patient encounter |

```bash
apx-agent agents scaffold my-coworker --template coworker   # writes an editable my-coworker/ project
```

See [docs/agents/coworker.md](docs/agents/coworker.md) for the full reference.

---

## Many agents — a governed fleet

Wiring is tolerable for one agent. For a fleet — agents calling each other across
apps, each hop needing auth, discovery, and reachability — it's the whole job.
That's the wiring apx-agent deletes. One agent declares another and calls it:

```python
# Local: compose in one process
investigation = SequentialAgent(agents=[presence, lineage, code, synthesis])

# Remote: call a sibling agent in its own app, over A2A
agent = Agent(
    instructions="Route to the right specialist.",
    sub_agents=["$DATA_TRIAGE_URL", "$BILLING_URL"],   # $VARs expand at startup
)
```

When you split an agent into its own app, the sub-agent call goes through the
**app-to-app auth path** — the caller's identity is passed through per hop, so a
downstream agent's tools still run under the *asking user's* UC grants, not a
shared service principal. Every deployed agent serves an [A2A discovery
card](docs/multi-agent/a2a.md) at `/.well-known/agent.json`, so sibling apps find
each other by probe, not by hardcoded config. `apx-agent doctor` reports whether
each declared sub-agent is actually reachable.

This is the layer the platform leaves open. Databricks
[Agent Services](https://docs.databricks.com/aws/en/ai-gateway/agent-services)
(Beta) registers agents in Unity Catalog for discovery and permissions — but its
own docs note "Runtime invocation is not available. Agents cannot be called
through a registered agent service." apx-agent is the runtime path: registered or
not, a declared agent can *call* another, governed, per hop.

Two examples ship this end-to-end:

| Example | Multi-agent shape |
|---|---|
| **data-triage-agent** | 6-step `SequentialAgent` (local) delegating SQL + Delta forensics to a **data-inspector** sub-agent in its own app **over A2A** |
| **customer_triage** | `HandoffAgent` over four specialists (triage / billing / account / technical) with principal-keyed memory recall surviving each handoff — Apps deploy verified live on `fe-stable` |

Pick the deploy boundary by lifecycle and consumers, not agent count — see
[docs/multi-agent/overview.md](docs/multi-agent/overview.md).

---

## See what you built

Every deployed agent ships with `/_apx/topology` — an interactive graph of agents, tools, sub-agents, and the UC / Genie / Vector Search / serving resources they reach. Click any node for its details.

![/_apx/topology — interactive graph of agents, tools, sub-agents, and platform resources](docs/images/topology-customer-triage.png)

See [docs/get-started/dev-ui.md](docs/get-started/dev-ui.md) for the full `/_apx/*` surface: chat, traces, eval, tool authoring in the Edit page's New Tool modal, probe.

---

## Examples

12 worked examples in [`python/examples/`](python/examples/EXAMPLES.md):

| Example | What it shows |
|---|---|
| **customer_triage** | `HandoffAgent` + memory + UC tools |
| **data-triage-agent** | 6-step `SequentialAgent` (presence → lineage → pipeline → genie → code → synthesis) |
| **entity-resolution-agent** | Fuzzy account match via Vector Search + `HandoffAgent` |
| **memory_demo** | `MemoryStore` + `ExampleStore` — recall across handoffs |
| **slack-agent** | Slack-initiated runs as the Slack user's Databricks identity |
| + 7 more | data-inspector, eligibility-agent, contract-parsing, shortage-intelligence, explain-my-bill, apx-builder, agent-hub |

---

## CLI

```bash
apx-agent agents scaffold <name>   # writes an editable <name>/ project directory
apx-agent agents run               # local FastAPI dev server (/_apx/agent) — run inside a scaffolded project
apx-agent agents deploy            # deploy the current project to Databricks Apps
apx-agent eval run evalset.jsonl   # run against deployed endpoint with LLM judge
apx-agent traces list --agent <name>   # recent MLflow traces filtered by apx.* attributes
apx-agent fleet list --where team=revops   # bulk ops across many agents (tag/backfill/repoint; dry-run by default)
                                   # repoint moves the @prod alias only (no rebuild); `fleet redeploy` is a deprecated alias
apx-agent label start --uc-name cat.sch.my_agent --judge domain_quality --scale 1-5 --assignee sme@co.com
                                   # open SME labeling session → prints Review App URL + run-id
apx-agent label align --uc-name cat.sch.my_agent --judge domain_quality --run <run-id>
                                   # align the judge on SME ratings (requires: pip install 'apx-agent[align]')
apx-agent doctor                   # diagnose auth, deps, project layout
```

See [docs/get-started/cli.md](docs/get-started/cli.md) for the full surface.

---

## Docs

| Topic | Doc |
|---|---|
| Quickstart | [docs/get-started/quickstart.md](docs/get-started/quickstart.md) |
| Running agents (`run`, `stream`, `max_iterations`) | [docs/agents/llm-agent.md](docs/agents/llm-agent.md) |
| DataAgent reference | [docs/agents/data-agent.md](docs/agents/data-agent.md) |
| CoworkerAgent reference | [docs/agents/coworker.md](docs/agents/coworker.md) |
| Agent composition | [docs/agents/composition.md](docs/agents/composition.md) |
| Routing (RouterAgent, HandoffAgent) | [docs/agents/routing.md](docs/agents/routing.md) |
| Tools — governed primitives | [docs/tools/overview.md](docs/tools/overview.md) |
| Tools — custom (`@tool`, MCP) | [docs/tools/custom-tools.md](docs/tools/custom-tools.md) |
| Multi-agent (sub-agents, A2A) | [docs/multi-agent/overview.md](docs/multi-agent/overview.md) |
| Sessions + memory | [docs/running/sessions-and-memory.md](docs/running/sessions-and-memory.md) |
| Guardrails and callbacks | [docs/safety/callbacks.md](docs/safety/callbacks.md) |
| Identity passthrough + OBO | [docs/safety/identity-passthrough.md](docs/safety/identity-passthrough.md) |
| Compliance (Watchdog, audit log) | [docs/safety/compliance.md](docs/safety/compliance.md) |
| Deploy targets | [docs/deploy/apps-vs-model-serving.md](docs/deploy/apps-vs-model-serving.md) |
| Scaffolded Apps CI/CD | [docs/deploy-cicd.md](docs/deploy-cicd.md) |
| Upgrade apx-agent pins safely | [docs/upgrade.md](docs/upgrade.md) |
| Evaluation | [docs/evaluate/overview.md](docs/evaluate/overview.md) |
| Configuration (`pyproject.toml`) | [docs/reference/configuration.md](docs/reference/configuration.md) |
| Coming from ADK or OpenAI Agents SDK | [docs/get-started/migration.md](docs/get-started/migration.md) |

---

## Coming from ADK or OpenAI Agents SDK?

See [docs/get-started/migration.md](docs/get-started/migration.md) for a concept-by-concept translation. The key mappings:

| ADK / OpenAI | apx-agent |
|---|---|
| `Agent(name, instructions, model)` | `LlmAgent(name, instructions)` or `Agent(...)` — set the model via the `[tool.apx.agent]` `model` field in `pyproject.toml` |
| `Runner.run()` | `run_once(agent, "prompt")` |
| `@function_tool` / `@tool` | `@tool` |
| `input_guardrails=[fn]` | `input_guardrails=[fn]` (same param name) |
| `@input_guardrail` tripwire | raise `PermissionError` in `before_agent_callback` |
| `before_tool_callback` | `before_tool` or `before_tool_callback` (both accepted) |
| `MemoryService` | `MemoryStore` |
| Handoffs | `HandoffAgent` |

---

## For AI coding assistants

The repo ships an [`llms.txt`](llms.txt) index of all documentation URLs. Add the docs as a local MCP server in Claude Code:

```bash
claude mcp add apx-agent-docs --transport stdio -- \
  uvx --from mcpdoc mcpdoc \
  --urls "apxAgent:https://raw.githubusercontent.com/stuagano/apx-agent/main/llms.txt" \
  --transport stdio
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
