# apx-agent

[![CI](https://github.com/stuagano/apx-agent/actions/workflows/test.yml/badge.svg)](https://github.com/stuagano/apx-agent/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Build governed Databricks agents. Three levels — use whichever fits:

| | |
|---|---|
| **`LlmAgent`** | The base. You own the loop: tools, hooks, guardrails, iteration cap. |
| **`DataAgent`** | One line over a Unity Catalog schema. Grounded in real columns, runs as the calling user. |
| **`CoworkerAgent`** | Joins two source systems. Persona, join key, objective. |

Deploy to Databricks Apps or Mosaic AI Model Serving — same agent, one flag.

---

## LlmAgent — you control the loop

`LlmAgent` is an LLM + tools + a loop. You decide what it can call, when it
stops, and what happens before and after each tool call. Nothing is hidden.

```python
from apx_agent import LlmAgent, uc_function_tool, genie_tool

agent = LlmAgent(
    instructions="Investigate customer accounts.",
    tools=[
        uc_function_tool("main.tools.lookup_account"),
        genie_tool("abc123", description="Answer billing questions"),
    ],
    max_iterations=10,
    before_tool=lambda name, args: print(f"calling {name}"),
    after_tool=lambda name, result: audit_log(name, result),
)
```

The loop knobs: `max_iterations` caps runaway chains. `before_tool` and
`after_tool` run synchronously around every tool call — use them for logging,
redaction, rate limiting, or hard stops. `input_guardrails` and
`output_guardrails` intercept at the message boundary. None of this requires
subclassing.

**Compose loops explicitly.** `LoopAgent` iterates until a condition is met;
`SequentialAgent` pipelines agents in order; `ParallelAgent` fans out;
`HandoffAgent` routes conversationally. These are first-class, not hacks
around a black-box framework.

```python
from apx_agent import SequentialAgent

investigation = SequentialAgent(
    agents=[presence_check, lineage_trace, code_analysis, synthesis],
    instructions="Investigate why data is missing.",
)
```

See [docs/workflow-patterns.md](docs/workflow-patterns.md) for the full composition reference.

---

## DataAgent — one line over a UC schema

```python
from apx_agent import DataAgent

agent = DataAgent("main", "sales")
```

That's a working agent. It knows the tables and columns in `main.sales`
before the first question — no `SHOW TABLES` at runtime, no discovery
prompt, no hallucinated schema.

**How schema discovery works (first match wins):**

1. **`apx scaffold` bakes `.apx/schema.json`** at project creation time by
   querying the UC Tables API. The manifest ships with your code, so a
   deployed app is grounded in the real columns with no `ws=` arg and no
   warehouse needed at startup.
2. **`ws=WorkspaceClient()`** triggers live introspection at construction
   time — useful when you want the freshest schema or are working outside a
   scaffolded project.
3. **`tables=` explicit override** — pass `{"orders": ["id(bigint)", ...]}` for
   tests or when you already have the schema from another source.
4. **Ungrounded fallback** — if none of the above applies, the agent falls
   back to generic data-assistant instructions and discovers the schema with
   its SQL tool on the first turn.

```python
# Live introspection — agent knows your columns at construction time:
from databricks.sdk import WorkspaceClient
agent = DataAgent("main", "sales", ws=WorkspaceClient())

# Add a persona, Genie, vector search, or UC functions on top:
agent = DataAgent(
    "main", "sales",
    persona="a revenue analyst",
    genie_space="abc123",
    vector_index="main.sales.product_docs",
    extra_tools=[uc_function_tool("main.tools.send_alert")],
)
```

**How governance works:** deploy once, everyone runs as themselves. The app
forwards each caller's OAuth token per request, and Unity Catalog enforces
their grants on their data. Share the app with your team — each person queries
their own slice. See [docs/identity-passthrough.md](docs/identity-passthrough.md).

See [docs/data-agent.md](docs/data-agent.md) for the full reference.

---

## CoworkerAgent — join two source systems

Two source systems landed in a UC schema. One business entity links them.
One question neither system can answer alone.

```python
from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
    join_key="employee ID",
    objective="surface mismatches between hours worked and paychecks issued",
    # memory="persistent",  # uncomment to remember facts across sessions
)
```

The `join_key` and `objective` are woven into the agent's grounded
instructions so it always knows which field links the tables and what question
it exists to answer. Common patterns:

| Use case | System A | System B | Join key |
|---|---|---|---|
| Payroll reconciliation | Kronos (hours worked) | Workday (paychecks) | employee ID |
| Quote-to-cash | Salesforce (deals) | NetSuite (invoices) | opportunity ID |
| Onboarding / offboarding | Workday (employment status) | Okta (access) | employee ID |
| Warranty & entitlement | ServiceNow (cases) | SAP (contracts, parts) | asset serial number |
| Order status | Oracle ERP (orders) | TMS (freight) | PO / shipment number |
| Claims integrity | Epic (chart) | Claims system (coding) | patient encounter |

Scaffold one:

```bash
apx scaffold my-coworker --template coworker
```

See [docs/coworker.md](docs/coworker.md) for the full reference.

---

## Deploy anywhere

```bash
apx deploy                                          # Databricks Apps (default)
apx deploy --target model-serving --name main.agents.my_agent
```

Same agent code. Same tools. Same `apx.*` MLflow trace schema either way. No
changes to `agent.py` to switch targets.

---

## See what you built

Every deployed agent ships with `/_apx/topology` — an interactive graph of
agents, tools, sub-agents, and the UC / Genie / Vector Search / serving
resources they reach. Click any node for its details.

![/_apx/topology — interactive graph of agents, tools, sub-agents, and platform resources](docs/images/topology-customer-triage.png)

See [docs/dev-ui.md](docs/dev-ui.md) for the full `/_apx/*` surface (chat, traces, eval, tool inspector, probe).

---

📚 **[Docs](docs/)** · 🚀 **[Quick start](#quick-start)** · 🧪 **[Examples](python/examples/EXAMPLES.md)** · ⚙️ **[CLI](#cli)**

---

## Quick start

Python 3.11+ required.

```bash
uv add "apx-agent[langgraph]"
uv run apx scaffold my-agent
cd my-agent && uv run apx run --reload   # FastAPI on :8000, /_apx/* dev UI
```

When it looks right: `uv run apx deploy`.

> **Something not working?** Run `apx doctor` — checks Python, uv, Databricks
> CLI, auth, and project layout. Prints a `Fix:` line for anything wrong.

See [docs/getting-started.md](docs/getting-started.md) for the full walkthrough including prerequisites, workspace requirements, and the local dev walk.

---

## Examples

13 worked examples in [`python/examples/`](python/examples/EXAMPLES.md):

| Example | What it shows |
|---|---|
| **customer_triage** | `HandoffAgent` + memory + UC tools |
| **data-triage-agent** | 6-step `SequentialAgent` (presence → lineage → pipeline → genie → code → synthesis) |
| **entity-resolution-agent** | Fuzzy account match via Vector Search + `HandoffAgent` |
| **memory_demo** | `MemoryBank` + `ExampleStore` — recall across handoffs |
| **voynich** | `LoopAgent` + 5-agent evolutionary population |
| **slack-agent** | Slack-initiated runs as the Slack user's Databricks identity |
| + 7 more | data-inspector, eligibility-agent, contract-parsing, shortage-intelligence, explain-my-bill, apx-builder, agent-hub |

---

## CLI

```bash
apx scaffold <name>                # scaffold a new agent project
apx run                            # local FastAPI dev server (/_apx/agent)
apx deploy                         # deploy to Databricks Apps (default)
apx deploy --target model-serving --name catalog.schema.agent
apx eval evalset.jsonl             # run against deployed endpoint with LLM judge
apx doctor                         # diagnose auth, deps, project layout
```

See [docs/cli.md](docs/cli.md) for the full surface.

---

## Docs

| Topic | Doc |
|---|---|
| DataAgent reference | [docs/data-agent.md](docs/data-agent.md) |
| CoworkerAgent reference | [docs/coworker.md](docs/coworker.md) |
| CoworkerAgent use cases | [docs/coworker-use-cases.md](docs/coworker-use-cases.md) |
| Workflow patterns + composition | [docs/workflow-patterns.md](docs/workflow-patterns.md) |
| Identity passthrough + OBO | [docs/identity-passthrough.md](docs/identity-passthrough.md) |
| Configuration — `pyproject.toml`, declarative tools | [docs/configuration.md](docs/configuration.md) |
| Sessions + memory + examples | [docs/sessions-and-memory.md](docs/sessions-and-memory.md) |
| Apps vs Model Serving | [docs/apps-vs-model-serving.md](docs/apps-vs-model-serving.md) |
| Governed primitives + UC functions | [docs/governed-primitives.md](docs/governed-primitives.md) |
| Evaluation + MLflow | [docs/evaluation.md](docs/evaluation.md) |
| Compliance — Watchdog, audit log, guards | [docs/compliance.md](docs/compliance.md) |
| Dev UI | [docs/dev-ui.md](docs/dev-ui.md) |

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
