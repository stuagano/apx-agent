# CoworkerAgent

A **CoworkerAgent** joins two disparate source systems landed in a Unity
Catalog schema. It's a `DataAgent` subclass that adds three coworker-specific
knobs — `persona`, `join_key`, and `objective` — and nothing else. The same
governed tools, the same UC identity passthrough, the same deploy path.

---

## The one-liner

```python
from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
    join_key="employee ID",
    objective="surface mismatches between hours worked and paychecks issued",
    memory="persistent",
)
```

That's the whole definition. Two required args (`catalog`, `schema`), three
identity knobs, one memory knob. Everything else — grounded instructions, SQL
tool, UC identity passthrough — is inherited from `DataAgent`.

---

## The three identity knobs

### `persona`

A plain string that gives the agent its role identity. Woven into the
schema-grounded instructions the `DataAgent` already builds.

```python
CoworkerAgent("main", "payroll", persona="a payroll operations analyst")
# → "You are a payroll operations analyst. You have access to
#    the following tables in main.payroll: ..."
```

No `PersonalityConfig` class. Just a string.

### `join_key`

The business entity that links the two source systems in the schema.
It tells the agent which field to join on when querying across tables.

```python
CoworkerAgent("main", "payroll", join_key="employee ID")
```

| | |
|---|---|
| `"employee ID"` | Payroll — Kronos × Workday |
| `"opportunity ID"` | Quote-to-Cash — Salesforce × NetSuite |
| `"asset serial number"` | Warranty — ServiceNow × SAP |
| `"patient encounter ID"` | Claims — Epic × clearinghouse |
| `"PO / shipment number"` | Order Status — Oracle ERP × TMS |

### `objective`

A plain string that defines *the question only the join answers* — the thing
neither source system can answer alone. When both `persona` and `objective`
are given, the lead becomes `"You are {persona} designed to {objective}."`:

```python
CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
    join_key="employee ID",
    objective="surface mismatches between hours worked and paychecks issued",
)
# → "You are a payroll operations analyst designed to surface mismatches
#    between hours worked and paychecks issued. The two source systems
#    are linked on employee ID. You have access to ..."
```

### `memory`

A one-word tier controlling both facts memory and session continuity:

| Value | What it means | Infra required |
|---|---|---|
| `"off"` | **Default.** Stateless — no memory tools wired | None |
| `"inmemory"` | Remembers within a single process run | None |
| `"persistent"` | Survives restarts via Lakebase (pgvector) | Lakebase instance |
| `"managed"` | Survives restarts via UC managed memory (long-term memory only) | UC catalog |
| `"lakebase"` (typed literally) | Raises — see below | — |

`"off"` is the default. Uncomment `memory="persistent"` to opt in — it
survives restarts on Lakebase. The knob defaults `host` to the `LAKEBASE_HOST`
env var and `embedding_model` to the always-available
`databricks-bge-large-en` FMAPI endpoint; if `LAKEBASE_HOST` is unset at boot,
memory/session degrade to no-op rather than crashing.

**Typing `memory="lakebase"` literally raises.** That's distinct from
`memory="persistent"` (which resolves to Lakebase with generated defaults) —
the bare `"lakebase"` value exists to catch a typo where you meant to declare
an explicit backend block yourself. To fully control the Lakebase connection
(non-default host/database/embedding model), pass `memory="off"` (or omit it)
and declare the backend explicitly in `pyproject.toml`:

```toml
[tool.apx.agent.memory]
type           = "lakebase"
host           = "my-lakebase.db.databricks.com"
database       = "agentdb"
embedding_model = "databricks-bge-large-en"
embedding_dim  = 1024
```

The upgrade path is: `off → inmemory → persistent`. Start with `off` (the
default) and add `memory="persistent"` when you want facts to survive
restarts; declare an explicit `[tool.apx.agent.memory]` block only when the
generated Lakebase defaults don't fit (custom host/database/embedding model),
or to use `"managed"` instead.

---

## What scaffold generates

```bash
apx-agent agents scaffold my-coworker --template coworker
```

By default this writes `my-coworker.yaml`, a deployable coworker spec. Deploy it with `apx-agent agents deploy my-coworker.yaml --target apps`.

If you want a local project directory first, add `--no-yaml`:

```bash
apx-agent agents scaffold my-coworker --template coworker --no-yaml
```

The project directory includes two files you care about:

**`agent.py`** — the agent definition:
```python
from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
    join_key="employee ID",
    objective="surface mismatches between hours worked and paychecks issued",
    # memory="persistent",  # uncomment to remember facts across sessions
    name="my-coworker",
)
```

**`pyproject.toml`** — the app envelope:
```toml
[tool.apx.agent]
name        = "my-coworker"
description = "An apx-agent on Databricks Apps."
model       = "databricks-claude-sonnet-4-6"
module      = "agent:agent"
```

Edit `agent.py` to point at your catalog/schema and set a persona. Don't
edit `agent_server/start_server.py` — that's framework boilerplate.

---

## The two-system join pattern

A coworker's data lives in Unity Catalog, not in the agent definition. Two
source systems land in one UC schema (via Lakeflow Connect or any ingestion),
and the coworker is grounded over the joined landing zone. The agent reasons
about the join; the lakehouse makes it physically possible.

**The template is the outline. The data fills in the colors.**

Every use case below is the same outline with a different `schema` and
`persona`:

```python
# Payroll — Kronos × Workday
agent = CoworkerAgent("main", "payroll",
    persona="a payroll operations analyst",
    objective="surface mismatches between hours worked and paychecks issued",
    memory="persistent")

# Quote-to-Cash — Salesforce × NetSuite
agent = CoworkerAgent("main", "revops",
    persona="a revenue operations analyst",
    objective="identify revenue leakage between closed deals and invoiced amounts",
    memory="persistent")

# Onboarding/Offboarding — Workday × Okta
agent = CoworkerAgent("main", "identity",
    persona="an IT onboarding and access analyst",
    objective="flag new hires without access and terminated employees still provisioned",
    memory="persistent")

# Warranty & Entitlement — ServiceNow × SAP
agent = CoworkerAgent("main", "service",
    persona="a warranty and entitlement analyst",
    objective="determine repair coverage and parts availability from contract and inventory data",
    memory="persistent")

# Order Status — ERP × TMS
agent = CoworkerAgent("main", "supply_chain",
    persona="a supply chain operations analyst",
    objective="reconcile order status, dock dates, and carrier invoices across ERP and TMS",
    memory="persistent")

# Claims Integrity — Epic × clearinghouse
agent = CoworkerAgent("main", "claims",
    persona="a claims integrity analyst",
    objective="explain claim denials and verify supporting documentation in the chart",
    memory="persistent")
```

One template, six coworkers. A new use case is a new landed schema and a
persona string — no new code.

---

## The join key pattern

Each use case above has the same structure:

| | |
|---|---|
| **System A** | Owns half the truth (time worked, deal terms, HR status, …) |
| **System B** | Owns the other half (payroll, invoices, access, …) |
| **Join key** | The business entity that links them (employee ID, opportunity ID, asset serial, …) |
| **The question** | Something neither system can answer alone |

The agent's value is surfacing mismatches: the paycheck that doesn't match
the hours, the terminated employee who still has access, the claim denied
despite documented care. Mismatches quantify directly in dollars — the
business case writes itself.

Memory compounds this: the coworker learns account-specific mapping quirks
(this customer's PO format, this carrier's reference codes) so the join gets
cheaper every turn.

---

## CoworkerAgent vs CoworkerTemplate

`CoworkerAgent` is the runtime agent — the Python object that answers
questions. You instantiate it directly in `agent.py`.

`CoworkerTemplate` is the factory registration — it's what `apx-agent agents scaffold
--template coworker` resolves, and what would let the framework build a
coworker from TOML alone without any Python. Today every coworker is a
Python one-liner; the template machinery is the seam for future config-only
builds.

---

## Further reading

- [`docs/reference/configuration.md`](../reference/configuration.md) — full `[tool.apx.agent.memory]` and `[tool.apx.agent.session]` field reference
- [`docs/running/sessions-and-memory.md`](../running/sessions-and-memory.md) — how memory and session stores work under the hood
- [`docs/agents/coworker-use-cases.md`](coworker-use-cases.md) — the five two-system join use cases in detail
- [`python/src/apx_agent/coworker.py`](../../python/src/apx_agent/coworker.py) — the implementation (it's short)
