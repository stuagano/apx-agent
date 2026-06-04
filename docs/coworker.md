# CoworkerAgent

A `DataAgent` that remembers — persona + persistent memory across sessions.

```python
from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
    memory="persistent",  # default
)
```

That's it. One line. Every conversation the same user has with this agent is connected — facts recalled from earlier sessions, session continuity restored on reconnect.

## What it actually is

`CoworkerAgent` **is a** `DataAgent` subclass. It adds exactly two knobs:

1. **`persona`** — a plain string woven into the grounded instructions the `DataAgent` builds from the UC schema. Tells the agent its role and voice.
2. **`memory`** — a one-word tier knob. Defaults to `"persistent"` (UC Delta). `DataAgent` defaults to `"off"`.

Everything else — schema grounding, SQL tool, UC function tools, identity passthrough — is inherited from `DataAgent`. See [`data-agent.md`](data-agent.md) for those details.

## Memory tiers

| `memory=` | Backend | Notes |
|---|---|---|
| `"off"` | none | stateless; default for plain `Agent` / `DataAgent` |
| `"inmemory"` (alias `"local"`) | in-process dict | survives turns, not restarts; good for development |
| `"persistent"` (alias `"delta"`) | UC Delta table | survives restarts and redeploys; default for `CoworkerAgent` |
| lakebase | pgvector | use explicit TOML blocks — one-word knob can't carry connection details |

`memory="persistent"` wires **two** stores at the same tier:
- **Fact memory** — long-term key/value facts the agent remembers across any session (`remember_fact`, `recall_facts`).
- **Session memory** — multi-turn conversation history, restored on reconnect (`save_session`, `load_session`).

Both stores are UC Delta tables, auto-created on first use. No extra infra, no extra config.

For lakebase (pgvector), use explicit `[tool.apx.agent.memory]` and `[tool.apx.agent.session]` blocks in `pyproject.toml` — see [`sessions-and-memory.md`](sessions-and-memory.md).

## Scaffold

```bash
apx scaffold my-coworker --template coworker
cd my-coworker && uv sync && uv run apx run
```

Generates two files:

```
my-coworker/
├── agent.py          ← edit this
└── pyproject.toml    ← ops envelope
```

`agent.py`:
```python
from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
)
```

`pyproject.toml` (key section):
```toml
[tool.apx.agent]
name = "my-coworker"
model = "databricks-claude-sonnet-4-6"
```

Point it at your schema, change the persona, run it.

## Two-system join pattern

The core value of `CoworkerAgent` is joining two systems of record in a single agent. The join key is a business entity; each system is authoritative for half the record.

```python
# Payroll: Kronos (time/attendance) × Workday (HR/pay)
agent = CoworkerAgent("main", "payroll", persona="a payroll operations analyst")

# RevOps: Salesforce (deals) × NetSuite (billing)
agent = CoworkerAgent("main", "revops", persona="a revenue operations analyst")

# IT: Workday (employment status) × Okta (access/accounts)
agent = CoworkerAgent("main", "it_compliance", persona="an IT compliance analyst")

# Field service: ServiceNow (cases) × SAP (contracts/parts)
agent = CoworkerAgent("main", "warranty", persona="a warranty entitlement agent")

# Supply chain: Oracle ERP (orders) × TMS (freight tracking)
agent = CoworkerAgent("main", "logistics", persona="a supply chain analyst")

# Healthcare: Epic (clinical docs) × claims clearinghouse (billing)
agent = CoworkerAgent("main", "revenue_cycle", persona="a revenue cycle analyst")
```

Same one-liner in every case. The UC schema holds the landed tables; the persona describes the role. See [`coworker-use-cases.md`](coworker-use-cases.md) for full use case detail.

## CoworkerAgent vs CoworkerTemplate

| | `CoworkerAgent` | `CoworkerTemplate` |
|---|---|---|
| What it is | A Python class (DataAgent subclass) | `@template` registry entry |
| How you use it | `agent.py` one-liner | `pyproject.toml` `template = "coworker"` block |
| User-facing today | **Yes** — this is the primary surface | Future — config-only path not yet served |
| Needs `agent.py` | Yes | No (once served) |

Today every coworker is a Python one-liner in `agent.py`. The `CoworkerTemplate` machinery is for a future config-only path where the entire coworker is declared in `pyproject.toml`. Use `CoworkerAgent` directly.

## Further reading

| Goal | Doc |
|---|---|
| Schema grounding, SQL tool, identity passthrough | [`data-agent.md`](data-agent.md) |
| Memory/session TOML reference (lakebase, Delta options) | [`sessions-and-memory.md`](sessions-and-memory.md) |
| Use cases — the two-system join catalog | [`coworker-use-cases.md`](coworker-use-cases.md) |
| pyproject.toml envelope | [`pyproject-toml.md`](pyproject-toml.md) |
