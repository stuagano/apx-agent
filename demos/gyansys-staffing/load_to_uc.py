# demos/gyansys-staffing/load_to_uc.py
"""Load synthetic data into gyansys_demo.staffing on fe-stable via a SQL warehouse.

Warehouse-only (serverless) — no Spark/Connect. Uses the Databricks SDK to find
a warehouse and the statement-execution API to create + populate tables.
"""
from __future__ import annotations

from databricks.sdk import WorkspaceClient

from generate_data import generate

# Catalog/schema/profile are workspace-specific. Built on the serverless-stable
# workspace, where this principal owns serverless_stable_qh44kx_catalog. Change
# these for other environments.
CATALOG = "serverless_stable_qh44kx_catalog"
SCHEMA = "gyansys_staffing"
PROFILE = "fevm-serverless-stable-qh44kx"


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
