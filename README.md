# apx-agent

[![CI](https://github.com/stuagano/apx-agent/actions/workflows/test.yml/badge.svg)](https://github.com/stuagano/apx-agent/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Build governed Databricks agents. Write a Python object — apx-agent compiles it to whichever Databricks runtime you target.

```bash
uv add apx-agent
uv run apx-agent agents scaffold my-agent
uv run apx-agent agents deploy my-agent.yaml --target apps
```

`scaffold` writes `my-agent.yaml` by default. `deploy` reads that spec, generates the Databricks Apps project at deploy time, and prints the App URL when done.

```bash
uv run apx-agent agents scaffold my-agent --no-yaml
cd my-agent && uv run apx-agent agents run --reload
```

Use `--no-yaml` when you want the editable project directory and local FastAPI dev UI first.

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
type = "delta"
table_name = "main.payroll.agent_memory"

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
| **Memory backends** | Delta Lake, Lakebase, or in-memory — same interface, declared not implemented |
| **Observation** | Tool calls, spans, and conversation deltas normalized before they reach any renderer |
| **Governance** | Identity passthrough, UC grants, and audit logging wired from the declaration |

You write a Python object or a TOML block. The normalization work is apx-agent's job.

---

Three agent types cover most use cases:

| | |
|---|---|
| **`LlmAgent`** | The base. You own the loop: tools, hooks, guardrails, iteration cap. |
| **`DataAgent`** | One line over a Unity Catalog schema. Grounded in real columns, runs as the calling user. |
| **`CoworkerAgent`** | Joins two source systems on a shared key. Persona, join key, objective. |

Deploy to Databricks Apps or Mosaic AI Model Serving — same agent definition, one flag changes the target.

---

## Quickstart

Python 3.11+ required.

**1. Install**

```bash
uv add apx-agent
```

**2. Scaffold a YAML spec**

```bash
uv run apx-agent agents scaffold my-agent
```

The default scaffold writes `my-agent.yaml` in the current directory. Fill in any `$CATALOG` / `$SCHEMA` placeholders before deploying.

**3. Deploy from YAML**

```bash
uv run apx-agent agents deploy my-agent.yaml --target apps
```

`deploy` generates the Apps project from the YAML in a temporary directory, bundles it, and creates a Databricks App. It prints the URL when done.

**4. Optional local project**

```bash
uv run apx-agent agents scaffold my-agent --no-yaml
cd my-agent && uv sync
uv run apx-agent agents run --reload
```

Use `--no-yaml` only when you need an editable project directory. FastAPI starts on `:8000`; chat at `/_apx/agent`, view traces at `/_apx/traces`, author tools from the **New Tool** modal on the Edit page (`/_apx/edit`) — the standalone `/_apx/tools` page is retired and redirects there. `agent.py` edits are picked up on restart — pass `--reload` (off by default) for auto-reload during local dev.

> **Something not working?** Run `uv run apx-agent doctor` — checks Python, uv, Databricks CLI, auth, and project layout. Prints a `Fix:` line for anything wrong.

See [docs/get-started/quickstart.md](docs/get-started/quickstart.md) for the full walkthrough.

---

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

---

## DataAgent — one line over a UC schema

```python
from apx_agent import DataAgent

agent = DataAgent("main", "sales")
```

That's a working agent. It knows the tables and columns in `main.sales` before the first question — no `SHOW TABLES` at runtime, no discovery prompt, no hallucinated schema.

Schema discovery priority (first match wins):

1. **Baked schema** — `.apx/schema.json`, written from the UC Tables API when the project is generated: `apx-agent agents scaffold --no-yaml`, or at `apx-agent agents deploy my-agent.yaml` time. Ships with your code.
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
apx-agent agents scaffold my-coworker --template coworker   # writes my-coworker.yaml; add --no-yaml for a local project
```

See [docs/agents/coworker.md](docs/agents/coworker.md) for the full reference.

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
apx-agent agents scaffold <name>   # writes <name>.yaml spec (add --no-yaml for a full project dir)
apx-agent agents run               # local FastAPI dev server (/_apx/agent) — run inside a --no-yaml project
apx-agent agents deploy <name>.yaml    # generate project from the spec + deploy to Databricks Apps
apx-agent eval run evalset.jsonl   # run against deployed endpoint with LLM judge
apx-agent traces list --agent <name>   # recent MLflow traces filtered by apx.* attributes
apx-agent fleet list --where team=revops   # bulk ops across many agents (tag/backfill/redeploy; dry-run by default)
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
