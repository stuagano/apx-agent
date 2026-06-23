# GyanSys Staffing Coworker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable, read-only GyanSys staffing CoworkerAgent on `fe-stable` that matches Salesforce opportunities to Replicon people via vector similarity, plus an architecture doc for the technical deep dive.

**Architecture:** Synthetic Salesforce + Replicon data loaded into UC (`gyansys_demo.staffing`); a Databricks Vector Search index over people skill-profiles; a single apx-agent `CoworkerAgent` declared entirely in one YAML spec (`vector_index` auto-wires nearest-neighbor matching, SQL tool is automatic and schema-grounded). Run locally via `apx-agent agents run <spec>.yaml`, then deploy to a Databricks App.

**Tech Stack:** apx-agent (CoworkerAgent), Databricks Vector Search (`databricks-gte-large-en` managed embeddings), Databricks SQL warehouse (serverless), `databricks-sql-connector`, `databricks-sdk`, Python 3.11+.

## Global Constraints

- Python 3.11+.
- Target workspace profile: `fe-stable` (every `databricks`/`apx-agent` command passes `--profile fe-stable`).
- UC location: catalog `gyansys_demo`, schema `staffing`.
- Read-only: NO write-back to Salesforce; the agent gets no write/mutation tools.
- Synthetic data only; deterministic (`random.seed(42)`, fixed `REFERENCE_DATE = date(2026, 7, 1)`).
- Embedding endpoint: `databricks-gte-large-en` (managed embeddings).
- Agent LLM: `databricks-claude-sonnet-4-6`.
- Demo project lives at `demos/gyansys-staffing/` in this repo.
- No tuple return types, no `x or ""` coercion, no `.get(key, "")`, no `getattr(obj, "literal", default)`, no `str = ""` defaults — the repo's pre-commit hooks reject these.
- Lean: predictive analytics, live connectors, multi-agent supervisor, and write-back are doc-only talking points, NOT built.

---

### Task 1: Demo project skeleton + UC catalog/schema

**Files:**
- Create: `demos/gyansys-staffing/README.md`
- (workspace) UC catalog `gyansys_demo`, schema `gyansys_demo.staffing` on `fe-stable`

**Interfaces:**
- Produces: the `gyansys_demo.staffing` UC schema that all later tasks read/write; the `demos/gyansys-staffing/` directory root.

- [ ] **Step 1: Create the project directory and README**

```bash
mkdir -p demos/gyansys-staffing
cat > demos/gyansys-staffing/README.md <<'EOF'
# GyanSys Staffing Coworker (demo)

Read-only Databricks demo: matches Salesforce opportunities to Replicon people
by skill similarity, surfaces stalled pipeline and staffing bandwidth.

Target workspace: `fe-stable`. UC location: `gyansys_demo.staffing`.

## Run order
1. `python generate_data.py` then `python load_to_uc.py`   (synthetic data → UC)
2. `python setup_vector_index.py`                            (people skill-profile VS index)
3. `uv run apx-agent agents run gyansys-staffing.yaml`       (local dev UI)
4. `uv run apx-agent agents deploy gyansys-staffing.yaml --target apps --profile fe-stable`

See ARCHITECTURE.md for the deep-dive talking points.
EOF
```

- [ ] **Step 2: Create the UC catalog and schema**

Run:
```bash
databricks catalogs create gyansys_demo --profile fe-stable 2>/dev/null || echo "catalog exists"
databricks schemas create staffing gyansys_demo --profile fe-stable 2>/dev/null || echo "schema exists"
```
Expected: no error (creates them, or reports they exist).

- [ ] **Step 3: Verify the schema exists**

Run: `databricks schemas get gyansys_demo.staffing --profile fe-stable`
Expected: JSON with `"full_name": "gyansys_demo.staffing"`.

- [ ] **Step 4: Commit**

```bash
git add demos/gyansys-staffing/README.md
git commit -m "feat(demo): gyansys-staffing project skeleton + UC schema"
```

---

### Task 2: Synthetic data generator (deterministic, TDD)

**Files:**
- Create: `demos/gyansys-staffing/generate_data.py`
- Test: `demos/gyansys-staffing/test_generate_data.py`

**Interfaces:**
- Produces: `generate(seed=42, reference_date=date(2026,7,1)) -> GeneratedData` where `GeneratedData` is a `@dataclass` with `opportunities: list[dict]` and `people: list[dict]`. Opportunity dicts have keys: `opportunity_id, name, account_name, stage, amount, probability, close_date, created_date, last_activity_date, region, required_role, required_skills, stall_reason`. People dicts have keys: `person_id, name, title, practice, region, skills, certifications, availability_pct, cost_rate, current_project`. (The derived `skill_profile` column is added in Task 3, not here.)

- [ ] **Step 1: Write the failing test**

```python
# demos/gyansys-staffing/test_generate_data.py
from __future__ import annotations

from datetime import date, timedelta

from generate_data import REFERENCE_DATE, generate


def _is_stalled(opp: dict) -> bool:
    open_stages = {"Prospecting", "Qualification", "Proposal", "Negotiation"}
    if opp["stage"] not in open_stages:
        return False
    age = (REFERENCE_DATE - opp["last_activity_date"]).days
    return age > 30


def test_counts_are_deterministic():
    a = generate()
    b = generate()
    assert len(a.people) == 200
    assert len(a.opportunities) == 75
    # deterministic: same seed → identical first rows
    assert a.people[0] == b.people[0]
    assert a.opportunities[0] == b.opportunities[0]


def test_planted_stalled_opportunities_exist():
    data = generate()
    stalled = [o for o in data.opportunities if _is_stalled(o)]
    assert len(stalled) >= 3
    # planted stalled opps are high-value and carry a reason
    assert any(o["amount"] >= 100_000 for o in stalled)
    assert all(o["stall_reason"] for o in stalled)


def test_india_is_tight_on_databricks_skill():
    data = generate()
    india_dbx = [
        p for p in data.people
        if p["region"] == "India" and "Databricks" in p["skills"]
    ]
    assert len(india_dbx) >= 1, "need at least one India Databricks person to match"
    # planted scarcity: their average availability is low (the bandwidth story)
    avg_avail = sum(p["availability_pct"] for p in india_dbx) / len(india_dbx)
    assert avg_avail < 30.0


def test_every_opportunity_has_required_skills():
    data = generate()
    assert all(o["required_skills"].strip() for o in data.opportunities)
    assert all(isinstance(o["required_skills"], str) for o in data.opportunities)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd demos/gyansys-staffing && python -m pytest test_generate_data.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'generate_data'`.

- [ ] **Step 3: Write the generator**

```python
# demos/gyansys-staffing/generate_data.py
"""Deterministic synthetic Salesforce + Replicon data for the GyanSys demo.

No external deps — stdlib only. `generate()` returns plain dicts so the loader
(Task 3) can write them to UC however it likes.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

REFERENCE_DATE = date(2026, 7, 1)

REGIONS = ["US", "India", "Philippines", "South America"]
SKILLS = [
    "Databricks", "PySpark", "Spark", "SQL", "Python", "Azure", "SAP",
    "Data Engineering", "Machine Learning", "Power BI", "Delta Lake",
    "Unity Catalog", "ETL", "Tableau", "dbt", "Kafka",
]
ROLES = [
    "Data Engineer", "Senior Data Engineer", "ML Engineer", "Data Architect",
    "BI Developer", "Analytics Lead", "Platform Engineer",
]
STAGES = ["Prospecting", "Qualification", "Proposal", "Negotiation",
          "Closed Won", "Closed Lost"]
OPEN_STAGES = {"Prospecting", "Qualification", "Proposal", "Negotiation"}
ACCOUNTS = [f"{a} {s}" for a in
            ["Northwind", "Globex", "Initech", "Umbrella", "Acme", "Soylent",
             "Hooli", "Stark", "Wayne", "Wonka"]
            for s in ["Corp", "Industries"]]
FIRST = ["Aarav", "Priya", "Maria", "Juan", "John", "Emily", "Wei", "Ana",
         "Carlos", "Divya", "Rahul", "Sofia", "Liam", "Noah", "Mia", "Raj"]
LAST = ["Patel", "Sharma", "Garcia", "Santos", "Smith", "Cruz", "Reyes",
        "Mendoza", "Kumar", "Silva", "Johnson", "Lopez", "Chen", "Das"]
CERTS = ["Databricks Certified Data Engineer Associate",
         "Databricks Certified ML Associate", "Azure Data Engineer",
         "AWS Solutions Architect", "(none)"]


@dataclass
class GeneratedData:
    opportunities: list[dict]
    people: list[dict]


def _name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"


def _people(rng: random.Random) -> list[dict]:
    people: list[dict] = []
    for i in range(200):
        region = rng.choice(REGIONS)
        title = rng.choice(ROLES)
        skills = rng.sample(SKILLS, k=rng.randint(3, 6))
        availability = float(rng.choice([0, 10, 20, 25, 40, 50, 60, 75, 90, 100]))
        people.append({
            "person_id": f"P{1000 + i}",
            "name": _name(rng),
            "title": title,
            "practice": "Analytics",
            "region": region,
            "skills": ", ".join(skills),
            "certifications": rng.choice(CERTS),
            "availability_pct": availability,
            "cost_rate": float(rng.randint(60, 220)),
            "current_project": rng.choice(["Bench", "Acme Migration",
                                           "Globex DW", "Internal R&D"]),
        })

    # Planted scarcity: India is tight on Databricks. EVERY India person who can
    # do Databricks work is near-fully allocated (the bandwidth story). Applying
    # this to all India+Databricks people — not just a few — is what makes the
    # "India is tight on Databricks" answer unambiguous.
    india_dbx = [p for p in people
                 if p["region"] == "India" and "Databricks" in p["skills"]]
    if not india_dbx:  # guarantee at least one for the demo
        forced = next(p for p in people if p["region"] == "India")
        forced["skills"] = "Databricks, PySpark, " + forced["skills"]
        india_dbx = [forced]
    for p in india_dbx:
        p["availability_pct"] = float(rng.choice([0, 10, 20]))
    return people


def _opportunities(rng: random.Random) -> list[dict]:
    opps: list[dict] = []
    for i in range(75):
        stage = rng.choice(STAGES)
        created = REFERENCE_DATE - timedelta(days=rng.randint(20, 300))
        # most opps have recent activity; some don't
        last_activity = REFERENCE_DATE - timedelta(days=rng.randint(0, 25))
        role = rng.choice(ROLES)
        req_skills = rng.sample(SKILLS, k=rng.randint(2, 4))
        opps.append({
            "opportunity_id": f"OPP{2000 + i}",
            "name": f"{rng.choice(ACCOUNTS)} — {role} engagement",
            "account_name": rng.choice(ACCOUNTS),
            "stage": stage,
            "amount": float(rng.randint(20, 500) * 1000),
            "probability": rng.randint(10, 90),
            "close_date": (REFERENCE_DATE + timedelta(days=rng.randint(10, 120))).isoformat(),
            "created_date": created.isoformat(),
            "last_activity_date": last_activity,  # date obj; serialized in loader
            "region": rng.choice(REGIONS),
            "required_role": role,
            "required_skills": ", ".join(req_skills),
            "stall_reason": "",
        })

    # Planted stalled opps: 4 high-value, open-stage, no activity > 30 days.
    reasons = ["Awaiting customer security review", "Budget approval pending",
               "Champion left the account", "Stuck on legal redlines"]
    for j in range(4):
        o = opps[j]
        o["stage"] = "Proposal"
        o["amount"] = float(rng.randint(120, 480) * 1000)
        o["last_activity_date"] = REFERENCE_DATE - timedelta(days=rng.randint(45, 120))
        o["stall_reason"] = reasons[j]
    return opps


def generate(seed: int = 42,
             reference_date: date = REFERENCE_DATE) -> GeneratedData:
    rng = random.Random(seed)
    return GeneratedData(
        opportunities=_opportunities(rng),
        people=_people(rng),
    )


if __name__ == "__main__":
    data = generate()
    stalled = [o for o in data.opportunities
               if o["stall_reason"] and o["stage"] in OPEN_STAGES]
    print(f"people={len(data.people)} opportunities={len(data.opportunities)} "
          f"stalled={len(stalled)}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd demos/gyansys-staffing && python -m pytest test_generate_data.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add demos/gyansys-staffing/generate_data.py demos/gyansys-staffing/test_generate_data.py
git commit -m "feat(demo): deterministic synthetic SF + Replicon data generator"
```

---

### Task 3: Load data into Unity Catalog

**Files:**
- Create: `demos/gyansys-staffing/load_to_uc.py`

**Interfaces:**
- Consumes: `generate()` → `GeneratedData` from Task 2.
- Produces: UC Delta tables `gyansys_demo.staffing.salesforce_opportunities` and `gyansys_demo.staffing.replicon_people`. `replicon_people` has a derived `skill_profile` STRING column (`title + " | " + skills + " | " + certifications`) and Change Data Feed enabled (required by the Task 4 delta-sync index).

- [ ] **Step 1: Write the loader**

```python
# demos/gyansys-staffing/load_to_uc.py
"""Load synthetic data into gyansys_demo.staffing on fe-stable via a SQL warehouse.

Warehouse-only (serverless) — no Spark/Connect. Uses the Databricks SDK to find
a warehouse and the statement-execution API to create + populate tables.
"""
from __future__ import annotations

from databricks.sdk import WorkspaceClient

from generate_data import generate

CATALOG = "gyansys_demo"
SCHEMA = "staffing"
PROFILE = "fe-stable"


def _wh_id(w: WorkspaceClient) -> str:
    for wh in w.warehouses.list():
        if wh.state and wh.state.value == "RUNNING":
            return wh.id
    # fall back to the first warehouse; caller starts it if needed
    first = next(iter(w.warehouses.list()), None)
    if first is None:
        raise RuntimeError("No SQL warehouse on fe-stable; create one first.")
    return first.id


def _sql(w: WorkspaceClient, wh: str, statement: str) -> None:
    res = w.statement_execution.execute_statement(
        warehouse_id=wh, statement=statement, catalog=CATALOG, schema=SCHEMA,
        wait_timeout="50s",
    )
    state = res.status.state.value if res.status and res.status.state else "?"
    if state != "SUCCEEDED":
        raise RuntimeError(f"SQL failed ({state}): {statement[:120]}...")


def _esc(value: str) -> str:
    return value.replace("'", "''")


def _insert_rows(w: WorkspaceClient, wh: str, table: str,
                 cols: list[str], rows: list[dict]) -> None:
    # batch multi-row INSERTs (200 rows / batch is well within limits)
    for start in range(0, len(rows), 200):
        chunk = rows[start:start + 200]
        values = []
        for r in chunk:
            cells = []
            for c in cols:
                v = r[c]
                if isinstance(v, (int, float)):
                    cells.append(str(v))
                else:
                    cells.append(f"'{_esc(str(v))}'")
            values.append("(" + ", ".join(cells) + ")")
        _sql(w, wh, f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
                    + ", ".join(values))


def main() -> None:
    w = WorkspaceClient(profile=PROFILE)
    wh = _wh_id(w)
    data = generate()

    # --- opportunities ---
    _sql(w, wh, "DROP TABLE IF EXISTS salesforce_opportunities")
    _sql(w, wh, """
        CREATE TABLE salesforce_opportunities (
          opportunity_id STRING, name STRING, account_name STRING,
          stage STRING, amount DOUBLE, probability INT, close_date STRING,
          created_date STRING, last_activity_date STRING, region STRING,
          required_role STRING, required_skills STRING, stall_reason STRING
        ) USING DELTA
    """)
    opp_cols = ["opportunity_id", "name", "account_name", "stage", "amount",
                "probability", "close_date", "created_date",
                "last_activity_date", "region", "required_role",
                "required_skills", "stall_reason"]
    opps = [{**o, "last_activity_date": o["last_activity_date"].isoformat()}
            for o in data.opportunities]
    _insert_rows(w, wh, "salesforce_opportunities", opp_cols, opps)

    # --- people (with derived skill_profile + CDF for vector sync) ---
    _sql(w, wh, "DROP TABLE IF EXISTS replicon_people")
    _sql(w, wh, """
        CREATE TABLE replicon_people (
          person_id STRING, name STRING, title STRING, practice STRING,
          region STRING, skills STRING, certifications STRING,
          availability_pct DOUBLE, cost_rate DOUBLE, current_project STRING,
          skill_profile STRING
        ) USING DELTA TBLPROPERTIES (delta.enableChangeDataFeed = true)
    """)
    people_cols = ["person_id", "name", "title", "practice", "region", "skills",
                   "certifications", "availability_pct", "cost_rate",
                   "current_project", "skill_profile"]
    people = [{**p, "skill_profile":
               f'{p["title"]} | {p["skills"]} | {p["certifications"]}'}
              for p in data.people]
    _insert_rows(w, wh, "replicon_people", people_cols, people)

    print(f"loaded {len(opps)} opportunities, {len(people)} people into "
          f"{CATALOG}.{SCHEMA}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the loader**

Run: `cd demos/gyansys-staffing && python load_to_uc.py`
Expected: `loaded 75 opportunities, 200 people into gyansys_demo.staffing`

- [ ] **Step 3: Verify the tables in UC**

Run:
```bash
databricks api post /api/2.0/sql/statements --profile fe-stable --json '{
  "warehouse_id": "REPLACE_WITH_WAREHOUSE_ID",
  "statement": "SELECT (SELECT count(*) FROM gyansys_demo.staffing.salesforce_opportunities) AS opps, (SELECT count(*) FROM gyansys_demo.staffing.replicon_people) AS people"
}'
```
Expected: result with `opps=75`, `people=200`. (Get the warehouse id from `databricks warehouses list --profile fe-stable`.)

- [ ] **Step 4: Commit**

```bash
git add demos/gyansys-staffing/load_to_uc.py
git commit -m "feat(demo): load synthetic data into gyansys_demo.staffing"
```

---

### Task 4: Vector Search index over people skill-profiles

**Files:**
- Create: `demos/gyansys-staffing/setup_vector_index.py`

**Interfaces:**
- Consumes: `gyansys_demo.staffing.replicon_people` (with `skill_profile` + CDF) from Task 3.
- Produces: a delta-sync Vector Search index `gyansys_demo.staffing.replicon_people_index` (primary key `person_id`, managed embeddings on `skill_profile` via `databricks-gte-large-en`), on endpoint `gyansys_demo_vs`. This exact index name is what Task 5's YAML `template.vector_index` references.

- [ ] **Step 1: Write the index setup script**

```python
# demos/gyansys-staffing/setup_vector_index.py
"""Create a VS endpoint + delta-sync index over replicon_people.skill_profile."""
from __future__ import annotations

import time

from databricks.sdk import WorkspaceClient

PROFILE = "fe-stable"
ENDPOINT = "gyansys_demo_vs"
SOURCE_TABLE = "gyansys_demo.staffing.replicon_people"
INDEX_NAME = "gyansys_demo.staffing.replicon_people_index"
EMBED_ENDPOINT = "databricks-gte-large-en"


def _ensure_endpoint(w: WorkspaceClient) -> None:
    from databricks.sdk.service.vectorsearch import EndpointType

    names = [e.name for e in w.vector_search_endpoints.list_endpoints()]
    if ENDPOINT not in names:
        w.vector_search_endpoints.create_endpoint(
            name=ENDPOINT, endpoint_type=EndpointType.STANDARD,
        )
    # Wait for ONLINE whether we just created it or it was already provisioning.
    # NOTE: a STANDARD endpoint can take 15-20 min to provision, and the index's
    # first delta-sync can lag the endpoint going "online" — poll the index
    # status generously (the demo build needed ~20 min end to end).
    w.vector_search_endpoints.wait_get_endpoint_vector_search_endpoint_online(
        endpoint_name=ENDPOINT,
    )
    print(f"endpoint {ENDPOINT} online")


def _ensure_index(w: WorkspaceClient) -> None:
    from databricks.sdk.service.vectorsearch import (
        DeltaSyncVectorIndexSpecRequest,
        EmbeddingSourceColumn,
        PipelineType,
        VectorIndexType,
    )

    existing = [i.name for i in w.vector_search_indexes.list_indexes(
        endpoint_name=ENDPOINT)]
    if INDEX_NAME in existing:
        print(f"index {INDEX_NAME} exists; syncing")
        w.vector_search_indexes.sync_index(index_name=INDEX_NAME)
        return

    w.vector_search_indexes.create_index(
        name=INDEX_NAME,
        endpoint_name=ENDPOINT,
        primary_key="person_id",
        index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=SOURCE_TABLE,
            pipeline_type=PipelineType.TRIGGERED,
            embedding_source_columns=[
                EmbeddingSourceColumn(
                    name="skill_profile",
                    embedding_model_endpoint_name=EMBED_ENDPOINT,
                ),
            ],
        ),
    )
    print(f"index {INDEX_NAME} created; waiting for first sync")
    # poll until the index reports ready
    for _ in range(60):
        idx = w.vector_search_indexes.get_index(index_name=INDEX_NAME)
        status = idx.status
        if status and status.ready:
            print("index ready")
            return
        time.sleep(15)
    raise RuntimeError("index did not become ready in time")


def main() -> None:
    w = WorkspaceClient(profile=PROFILE)
    _ensure_endpoint(w)
    _ensure_index(w)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the setup**

Run: `cd demos/gyansys-staffing && python setup_vector_index.py`
Expected: ends with `index ready` (endpoint creation may take several minutes the first time).

- [ ] **Step 3: Verify nearest-neighbor matching works**

Run:
```bash
cd demos/gyansys-staffing && python -c "
from databricks.sdk import WorkspaceClient
w = WorkspaceClient(profile='fe-stable')
r = w.vector_search_indexes.query_index(
    index_name='gyansys_demo.staffing.replicon_people_index',
    query_text='Databricks PySpark data engineering',
    columns=['person_id','name','region','skills','availability_pct'],
    num_results=5)
print(r.result.data_array if r.result else 'NO RESULTS')
"
```
Expected: 5 rows; the top hits have Databricks/PySpark in `skills`.

- [ ] **Step 4: Commit**

```bash
git add demos/gyansys-staffing/setup_vector_index.py
git commit -m "feat(demo): vector search index over replicon_people skill profiles"
```

---

### Task 5: The CoworkerAgent spec (declarative YAML)

**Files:**
- Create: `demos/gyansys-staffing/gyansys-staffing.yaml`

**Interfaces:**
- Consumes: `gyansys_demo.staffing` tables (Task 3) and `gyansys_demo.staffing.replicon_people_index` (Task 4).
- Produces: a runnable apx-agent spec. `template.vector_index` auto-wires the nearest-neighbor `vector_search` tool; the SQL tool is automatic and grounded by the catalog/schema. No custom Python.

- [ ] **Step 1: Write the spec**

```yaml
# demos/gyansys-staffing/gyansys-staffing.yaml
name: gyansys-staffing
description: >
  Read-only staffing coworker. Matches Salesforce opportunities to Replicon
  people by skill similarity, and surfaces stalled pipeline and bandwidth.

model: databricks-claude-sonnet-4-6

instructions: >
  You are a resource-planning analyst for practice leadership at a ~200-person
  analytics consultancy. Salesforce opportunities live in
  gyansys_demo.staffing.salesforce_opportunities; Replicon people (skills,
  certifications, availability_pct, region, cost_rate) live in
  gyansys_demo.staffing.replicon_people. To recommend people for an
  opportunity, use the vector_search tool with the opportunity's required role
  and skills, then prefer higher availability_pct and matching region; always
  name the people, their region, key skills, and availability. An opportunity
  is "stalled" when it is in an open stage (Prospecting, Qualification,
  Proposal, Negotiation) and last_activity_date is more than 30 days before
  2026-07-01; cite stall_reason. Answer in management terms (fit, bandwidth,
  pipeline health), never invent columns, and never claim to modify Salesforce
  — this is read-only.

examples:
  - "Who are the best-fit available people for the highest-value stalled opportunity?"
  - "Which opportunities are stalled, and why?"
  - "How much availability do we have for Databricks work in India?"

template:
  name: coworker
  catalog: gyansys_demo
  schema: staffing
  persona: a resource-planning analyst for practice leadership
  join_key: required skills (matched via vector similarity, not a shared ID)
  objective: >
    Match opportunities and leads to the best-fit available people, and surface
    stalled pipeline and staffing bandwidth across regions.
  vector_index: gyansys_demo.staffing.replicon_people_index
  memory: "off"
  include_functions: false

guardrails:
  injection_detection: false
  blocked_tools: []
  rate_limit: null

tools: []
```

- [ ] **Step 2: Verify the spec is valid and shows the instructions**

Run: `cd demos/gyansys-staffing && uv run apx-agent agents describe gyansys-staffing.yaml`
Expected: prints `name: gyansys-staffing`, the `template: coworker (gyansys_demo.staffing)` line, and the full Instructions block (no "empty" hint).

- [ ] **Step 3: Commit**

```bash
git add demos/gyansys-staffing/gyansys-staffing.yaml
git commit -m "feat(demo): gyansys staffing coworker agent spec"
```

---

### Task 6: Run locally and validate the demo answers (acceptance)

**Files:**
- Modify: `demos/gyansys-staffing/README.md` (add verified example transcript)

**Interfaces:**
- Consumes: everything from Tasks 3–5, live on `fe-stable`.
- Produces: a verified, runnable local demo — the acceptance bar from the spec.

- [ ] **Step 1: Start the agent locally**

Run: `cd demos/gyansys-staffing && DATABRICKS_CONFIG_PROFILE=fe-stable uv run apx-agent agents run gyansys-staffing.yaml --port 8080`
Expected: `# Generated a local project from gyansys-staffing.yaml ...`, `Application startup complete`, server on `:8080`. Leave it running in a second shell.

- [ ] **Step 2: Verify the matching answer**

Run:
```bash
curl -s -X POST http://127.0.0.1:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Who are the best-fit available people for the highest-value stalled opportunity?"}]}' | tail -c 1200
```
Expected: a grounded answer that names a stalled high-value opportunity and lists specific people (names + region + skills + availability) — i.e. the vector tool + SQL tool were used.

- [ ] **Step 3: Verify the stalled-pipeline answer**

Run:
```bash
curl -s -X POST http://127.0.0.1:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Which opportunities are stalled, and why?"}]}' | tail -c 1200
```
Expected: lists the planted stalled opps (Proposal stage, >30 days idle) with their `stall_reason`.

- [ ] **Step 4: Verify the bandwidth answer**

Run:
```bash
curl -s -X POST http://127.0.0.1:8080/invocations \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"How much availability do we have for Databricks work in India?"}]}' | tail -c 1200
```
Expected: reports the India Databricks scarcity (low availability) — the planted bandwidth story.

- [ ] **Step 5: Record the verified transcript and commit**

Append the three Q&A pairs (trimmed) to `README.md` under a "Verified demo answers" heading, then:
```bash
git add demos/gyansys-staffing/README.md
git commit -m "docs(demo): verified local demo transcript for gyansys staffing"
```

---

### Task 7: Architecture doc + deep-dive talking points

**Files:**
- Create: `demos/gyansys-staffing/ARCHITECTURE.md`

**Interfaces:**
- Consumes: the as-built demo (Tasks 1–6) as the concrete reference.
- Produces: the deep-dive document mapping customer asks → the build, plus doc-only sections.

- [ ] **Step 1: Write the architecture doc**

Write `demos/gyansys-staffing/ARCHITECTURE.md` containing, each as its own section with real content (no placeholders):
1. **Phase-1 data flow** — the diagram from the spec (SF + Replicon → UC → VS index → CoworkerAgent → App), noting ingestion is synthetic seed today and Lakeflow Connect (SF) + batch sync (Replicon) is where live ingestion plugs in.
2. **Databricks Apps fit** — the App is the internal read-only front end; mgmt reads pipeline without a Salesforce seat (the license-cost win).
3. **Genie & grounding** — the agent is schema-grounded (real columns, no invented fields); Genie can be added as a `type: genie` tool for open-ended NL; the vector index is the retrieval layer that addresses hallucination on the matching.
4. **Predictive (doc-only)** — where an MLflow stall-risk / profitability model would slot in over historical opportunities; not built in phase 1.
5. **Cost** — serverless SQL + a ~200-row VS index is cheap; quote it against Salesforce license expansion and traditional hosted apps.
6. **Platform boundaries (doc-only)** — external/vendor/attorney access, multi-tenant, transactional (React/Python/Postgres alternative) as phase-2+ evaluation.
7. **Demo script** — the customer-ask → agent-action table from the spec, with the actual verified answers from Task 6.

- [ ] **Step 2: Commit**

```bash
git add demos/gyansys-staffing/ARCHITECTURE.md
git commit -m "docs(demo): gyansys phase-1 architecture + deep-dive talking points"
```

---

### Task 8: Deploy to a Databricks App (with known fe-stable bundle risk)

**Files:**
- Modify: `demos/gyansys-staffing/README.md` (record App URL or the local-only fallback decision)

**Interfaces:**
- Consumes: the validated spec (Task 5) and live data/index on `fe-stable`.
- Produces: a deployed Databricks App serving the agent — OR a documented local-only fallback if the bundle issue blocks.

- [ ] **Step 1: Attempt the deploy**

Run: `cd demos/gyansys-staffing && uv run apx-agent agents deploy gyansys-staffing.yaml --target apps --profile fe-stable`
Expected: builds, bundles, creates the App, prints the App URL.

- [ ] **Step 2: If it fails with a bundle/workspace mismatch**

Known footgun: the bundle state has pointed at `fe-cowork` (workspace_id / home-dir mismatch). Diagnose:
```bash
databricks current-user me --profile fe-stable        # confirm the home dir / workspace
find demos/gyansys-staffing -name 'databricks.yml' -o -name '*.bundle.*' 2>/dev/null
```
If a generated `databricks.yml`/bundle references `fe-cowork` or a wrong `workspace_id`, correct the target host/workspace to fe-stable's and re-run Step 1. If it still blocks, STOP and fall back: keep the demo local-only (Task 6 is the deliverable), and record the blocker.

- [ ] **Step 3: Verify the deployed app (only if deployed)**

Run: `curl -s "<APP_URL>/health"` (App URL from Step 1)
Expected: HTTP 200. Then POST one question to `<APP_URL>/invocations` and confirm a grounded answer (same shape as Task 6).

- [ ] **Step 4: Record outcome and commit**

In `README.md`, record either the live App URL (deployed) or a short "Local-only for now — App deploy blocked by <reason>" note. Then:
```bash
git add demos/gyansys-staffing/README.md
git commit -m "docs(demo): record gyansys app deployment outcome"
```

---

## Optional Task 9: Add a Genie space tool

Only if you want to showcase Genie specifically in the deep dive (the spec marks this optional).

- [ ] Create a Genie space over `gyansys_demo.staffing` in the workspace UI; note its `space_id`.
- [ ] Add to `gyansys-staffing.yaml` under `tools:`:
  ```yaml
  tools:
    - type: genie
      space_id: "<SPACE_ID>"
      name: ask_staffing_data
      description: "Open-ended natural-language questions over staffing + pipeline data"
  ```
- [ ] Re-run Task 6 Step 1 and confirm `apx-agent agents describe gyansys-staffing.yaml` now lists the genie tool.
- [ ] Commit: `git commit -am "feat(demo): optional Genie space tool for gyansys staffing"`
