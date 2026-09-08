# BrickRoad + EE Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local apx-agent example with two tools — a BrickRoad Genie wrapper and a general SQL tool — that Stuart can chat with locally to explore BrickRoad customer blockers and EE UCO pipeline/forecast data.

**Architecture:** A plain `Agent` (not `HandoffAgent` — single agent, no routing needed) with two pre-built SDK tool factories: `genie_tool()` wrapping the BrickRoad Genie space, and `sql_tool()` scoped by description to four known table paths. Runs locally via `apx-agent run` against the `logfood` Databricks CLI profile — no Databricks Apps deployment (see design doc for why).

**Tech Stack:** apx-agent SDK (`Agent`, `genie_tool`, `sql_tool`, `create_app`), FastAPI/uvicorn (for `apx-agent run`'s local server), pytest for smoke tests.

## Global Constraints

- Python >=3.11 (repo-wide floor).
- No new tool-calling logic — both tools are pre-built SDK factories (`genie.py`, `sql_tools.py`). This plan is wiring only.
- Local only. Do not add `databricks.yml` / Apps deploy scaffolding — that's explicitly out of scope per the design doc (`docs/superpowers/specs/2026-07-10-brickroad-ee-agent-design.md`).
- Repo verification gate: `make check` (from repo root) must stay green after these changes — run it as part of Task 2.
- Follow the existing example layout convention (see `python/examples/customer_triage/`): `agent.py` at the example root exports a module-level `agent`, `app.py` wraps it via `create_app`, tests live in `tests/test_smoke.py` and do pure import-time/introspection checks (no live Databricks calls).

---

### Task 1: Scaffold the example project

**Files:**
- Create: `python/examples/brickroad-ee-agent/pyproject.toml`
- Create: `python/examples/brickroad-ee-agent/.env.example`
- Create: `python/examples/brickroad-ee-agent/.gitignore`
- Create: `python/examples/brickroad-ee-agent/app.py`
- Create: `python/examples/brickroad-ee-agent/README.md`

**Interfaces:**
- Produces: a `python/examples/brickroad-ee-agent/` directory that `uv sync` can install, and an `app.py` that imports `agent` from a (not-yet-created) `agent.py` — Task 2 creates that module.

- [ ] **Step 1: Create the project directory and pyproject.toml**

```bash
mkdir -p ~/Documents/apx-agent/python/examples/brickroad-ee-agent/tests
```

Write `python/examples/brickroad-ee-agent/pyproject.toml`:

```toml
[project]
name = "brickroad-ee-agent"
version = "0.1.0"
description = "apx-agent — explore BrickRoad customer blockers and EE UCO pipeline/forecast data. Local only, not deployed."
requires-python = ">=3.11"
dependencies = [
    "apx-agent",
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "python-dotenv>=1.0.0",
]

[tool.uv.sources]
apx-agent = { path = "../..", editable = true }

[tool.apx.agent]
name = "brickroad_ee_agent"
description = "Explore BrickRoad customer blockers and EE UCO pipeline/forecast data."
model = "databricks-claude-sonnet-4-6"
max_iterations = 8
module = "agent:agent"

[dependency-groups]
dev = [
    "pytest>=9.0.3",
]
```

- [ ] **Step 2: Write the env template**

Write `python/examples/brickroad-ee-agent/.env.example`:

```
DATABRICKS_CONFIG_PROFILE=logfood
BRICKROAD_GENIE_SPACE_ID=01f13ee082c41985a98795628dd47d7c
```

- [ ] **Step 3: Write the gitignore**

Write `python/examples/brickroad-ee-agent/.gitignore`:

```
__pycache__/
*.pyc
.venv/
.env
.env.local
.databricks/
```

- [ ] **Step 4: Write the FastAPI entry point**

Write `python/examples/brickroad-ee-agent/app.py`:

```python
"""FastAPI app — uvicorn entry point for ``apx-agent run``."""

from apx_agent import create_app

from agent import agent

app = create_app(agent)
```

- [ ] **Step 5: Write the README**

Write `python/examples/brickroad-ee-agent/README.md`:

```markdown
# brickroad-ee-agent

Local-only apx-agent for exploring two data sources relevant to EE
(Emerging Enterprise): BrickRoad customer blockers, and the EE UCO
pipeline/forecast data in Logfood Unity Catalog.

Not deployed to Databricks Apps — see
`docs/superpowers/specs/2026-07-10-brickroad-ee-agent-design.md` in the
repo root for why (no deploy rights on the workspace that has this data;
the alternative workspace is on a different UC metastore with no path to
it).

## Setup

```bash
cp .env.example .env   # defaults are already correct, but review
uv sync
databricks auth login --host https://adb-2548836972759138.18.azuredatabricks.net --profile logfood
databricks current-user me --profile logfood   # must succeed
```

## Run

```bash
uv run apx-agent doctor   # verify setup
uv run apx-agent run      # starts a local chat server
```

## Tools

- **`ask_brickroad`** — natural-language questions over BrickRoad customer
  blockers/blindspots (Genie space `01f13ee082c41985a98795628dd47d7c`).
- **`run_sql`** — ad-hoc SQL against `home_stuart_gano.ee_rollups.uco_snapshot`,
  `main.it_brick_road.*`, `main.gtm_gold.rpt_individual_obt_gtm_whales`, and
  `main.gtm_silver.individual_hierarchy_salesforce`. Runs as your own
  identity — only tables you have UC grants on are queryable.

Example questions:
- "What are the top BrickRoad blindspots this month?"
- "Which U4 UCOs in my EE snapshot have open BrickRoad blockers?"
  (joins `uco_snapshot.UCO_ID` to `main.it_brick_road.issue_use_cases`)
```

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/apx-agent
git add python/examples/brickroad-ee-agent/pyproject.toml \
        python/examples/brickroad-ee-agent/.env.example \
        python/examples/brickroad-ee-agent/.gitignore \
        python/examples/brickroad-ee-agent/app.py \
        python/examples/brickroad-ee-agent/README.md
git commit -m "scaffold(examples): brickroad-ee-agent project files"
```

---

### Task 2: Write agent.py and smoke tests

**Files:**
- Create: `python/examples/brickroad-ee-agent/tests/test_smoke.py`
- Create: `python/examples/brickroad-ee-agent/agent.py`

**Interfaces:**
- Consumes: `apx_agent.Agent`, `apx_agent.genie_tool(space_id, *, name, description)`, `apx_agent.sql_tool(*, name, description)`, `apx_agent.collect_resource_specs(agent)` — all existing SDK exports, no changes needed to `apx_agent` itself.
- Produces: `agent.py` exports a module-level `agent: Agent` and module-level `ask_brickroad`, `run_sql` tool callables — `app.py` (Task 1) imports `agent` from this module.

- [ ] **Step 1: Write the failing smoke tests**

Write `python/examples/brickroad-ee-agent/tests/test_smoke.py`:

```python
"""Smoke tests for the brickroad-ee-agent example.

Pure import-time + introspection checks — no live Databricks calls.

Run from this directory::

    pytest tests/
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _add_example_to_path() -> None:
    """Make agent.py importable from the example directory."""
    example_dir = Path(__file__).parent.parent
    if str(example_dir) not in sys.path:
        sys.path.insert(0, str(example_dir))
    yield


def test_agent_imports() -> None:
    import agent as agent_module
    assert hasattr(agent_module, "agent"), "expected a top-level `agent` variable"


def test_agent_is_llm_agent() -> None:
    import agent as agent_module
    from apx_agent import Agent

    assert isinstance(agent_module.agent, Agent)


def test_both_tools_exported_with_expected_names() -> None:
    import agent as agent_module

    assert agent_module.ask_brickroad.__name__ == "ask_brickroad"
    assert agent_module.run_sql.__name__ == "run_sql"


def test_declared_resources_include_genie_space() -> None:
    import agent as agent_module
    from apx_agent import collect_resource_specs

    specs = collect_resource_specs(agent_module.agent)
    kinds = {s.kind for s in specs}
    assert "genie_space" in kinds


def test_brickroad_space_id_matches_default() -> None:
    import agent as agent_module

    assert agent_module.BRICKROAD_GENIE_SPACE_ID == "01f13ee082c41985a98795628dd47d7c"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd ~/Documents/apx-agent/python/examples/brickroad-ee-agent
uv sync
uv run pytest tests/ -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'agent'` (agent.py doesn't exist yet).

- [ ] **Step 3: Write agent.py**

Write `python/examples/brickroad-ee-agent/agent.py`:

```python
"""brickroad-ee-agent — explore BrickRoad customer blockers and EE UCO
pipeline/forecast data via chat.

Local-only (see docs/superpowers/specs/2026-07-10-brickroad-ee-agent-design.md
in the repo root) — run with ``apx-agent run`` against the ``logfood``
Databricks CLI profile. Not deployed to Databricks Apps: Logfood (where all
this data lives) has no deploy rights for this user, and the alternative
workspace (FE Stable) is on a different UC metastore entirely with no path
to this data.
"""
from __future__ import annotations

import os

from apx_agent import Agent, genie_tool, sql_tool

BRICKROAD_GENIE_SPACE_ID = os.environ.get(
    "BRICKROAD_GENIE_SPACE_ID", "01f13ee082c41985a98795628dd47d7c"
)

ask_brickroad = genie_tool(
    BRICKROAD_GENIE_SPACE_ID,
    name="ask_brickroad",
    description=(
        "Ask a natural-language question about BrickRoad customer blockers, "
        "issues, blindspots (issues with no matched feature), and "
        "feature-matching quality. Answers come from BrickRoad's own Genie "
        "space, grounded in main.it_brick_road.*."
    ),
)

run_sql = sql_tool(
    name="run_sql",
    description=(
        "Run a SQL query against Logfood Unity Catalog tables relevant to "
        "EE (Emerging Enterprise) pipeline and blockers. Runs as the "
        "calling user - only tables you have UC grants on are queryable. "
        "Known-useful tables and their join keys:\n"
        "  - home_stuart_gano.ee_rollups.uco_snapshot - corrected, "
        "historized EE UCO active-pipeline snapshot (U2-U5), one row per "
        "UCO per snapshot_date, refreshed weekly. Key: UCO_ID.\n"
        "  - main.it_brick_road.issues / .solutions / .issue_use_cases - "
        "BrickRoad's native tables. issue_use_cases maps BrickRoad issue "
        "IDs to Salesforce UCO IDs directly - join to uco_snapshot.UCO_ID "
        "to cross-reference blockers against pipeline.\n"
        "  - main.gtm_gold.rpt_individual_obt_gtm_whales - per-user "
        "per-quarter forecast (one row per user_id x fiscal_year_quarter). "
        "CAVEAT: source dashboard is tagged 'UAT Hub 2.0' - treat as "
        "possibly pre-production, not a fully-blessed number.\n"
        "  - main.gtm_silver.individual_hierarchy_salesforce - org "
        "hierarchy (CROminus1..7name/email columns) and segment "
        "(Region_Level_1..4, AE_segment). Joins to the forecast table on "
        "user_id/email."
    ),
)

agent = Agent(
    name="brickroad_ee_agent",
    instructions=(
        "You help Stuart explore two things: BrickRoad customer blockers "
        "(use ask_brickroad for narrative answers) and EE "
        "(Emerging Enterprise) pipeline/forecast data (use run_sql).\n\n"
        "When asked a cross-cutting question - e.g. 'which at-risk UCOs "
        "have open BrickRoad blockers' - use both tools: query "
        "home_stuart_gano.ee_rollups.uco_snapshot for the UCOs, join "
        "against main.it_brick_road.issue_use_cases on UCO_ID to find "
        "matching BrickRoad issues, then optionally use ask_brickroad for "
        "narrative detail on a specific issue.\n\n"
        "Always state which table/tool a number came from. Flag when a "
        "number comes from main.gtm_gold.rpt_individual_obt_gtm_whales "
        "since that source is UAT-tagged, not fully blessed."
    ),
    tools=[ask_brickroad, run_sql],
)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd ~/Documents/apx-agent/python/examples/brickroad-ee-agent
uv run pytest tests/ -v
```

Expected: PASS — all 5 tests green.

- [ ] **Step 5: Run the repo-wide verify gate to confirm no regression**

```bash
cd ~/Documents/apx-agent
make check
```

Expected: exits 0. If it fails on something unrelated to this change (pre-existing failure), note it but don't fix it as part of this task — out of scope.

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/apx-agent
git add python/examples/brickroad-ee-agent/agent.py \
        python/examples/brickroad-ee-agent/tests/test_smoke.py
git commit -m "feat(examples): brickroad-ee-agent — Genie + SQL tools over BrickRoad and EE data"
```

---

### Task 3: Manual end-to-end verification

**Files:** none (no code changes — this is the ctk "read after write" proof for behavior that can't be unit tested: live Genie/SQL calls and real LLM tool selection).

**Interfaces:**
- Consumes: the running `agent` from Task 2, a live `logfood` Databricks session.

- [ ] **Step 1: Authenticate and verify setup**

```bash
cd ~/Documents/apx-agent/python/examples/brickroad-ee-agent
databricks auth login --host https://adb-2548836972759138.18.azuredatabricks.net --profile logfood
uv run apx-agent doctor
```

Expected: doctor reports Python/uv/Databricks CLI/auth all OK.

- [ ] **Step 2: Start the local server**

```bash
uv run apx-agent run
```

Expected: server starts, prints a local URL (e.g. `http://localhost:8000`).

- [ ] **Step 3: Ask one question per tool, confirm real answers**

Via the local chat UI/API at the printed URL, ask:
1. "What are the top BrickRoad blindspots right now?" — confirms `ask_brickroad` reaches the Genie space and returns a real narrative answer (not an error).
2. "How many UCOs are in my EE snapshot at Stage U4, and what's their total MRR?" — confirms `run_sql` can query `home_stuart_gano.ee_rollups.uco_snapshot` and return real numbers (compare against the known baseline: 3,555 total rows / ~$12.71M MRR across all stages from the 2026-07-10 snapshot — the U4-only subset should be a plausible fraction of that).

Expected: both return real, non-error answers grounded in actual data, not hallucinated or refused.

- [ ] **Step 4: Record the outcome**

If either tool fails (auth error, permission error, empty result), stop and diagnose before considering this plan complete — don't paper over a live failure. If both succeed, the plan is done; no further commit needed (Task 3 produces no file changes).

---

### Task 4: Add Databricks discovery extensions

The vendor-neutral six-step discovery workflow already lives in
`python/src/apx_agent/discovery/`. Keep Databricks-specific research and
handoffs in this example rather than adding product vocabulary to the core
workflow.

**Files:** define the final module layout when implementation starts; reuse the
existing `DiscoveryWorkflow`, `ResearchProvider`, `ask_brickroad`, and
`run_sql` interfaces.

- [ ] Implement `DatabricksResearchProvider` using `ask_brickroad` and
  `run_sql` to assemble current account, blocker, pipeline, forecast, and org
  context.
- [ ] Register a `wow_to_ucos` handoff that maps selected ideas to relevant
  UCOs.
- [ ] Register a `solution_builder` handoff that turns a selected idea into a
  Databricks solution outline.
- [ ] Register a `fetch_references` handoff for grounded reference material.
- [ ] Register a `quantify_value` handoff for Guided Value Platform-style
  value estimates.
- [ ] Add deterministic tests for provider injection, handoff order, resume,
  and structured outputs. Keep live Genie and SQL validation in the manual
  verification step and always pass an explicit Databricks CLI profile.
