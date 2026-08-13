"""Land the Mirion pre-call synthetic data + governed views on fevm-hvhhmh.

Creates schema, one source table per view (loaded with synthetic rows), and the
7 governed views over them. Catalog is the sandbox-writable managed catalog;
the committed sql/vw_*.sql (which target `main`) stay untouched — this is the
sandbox instance's catalog value, config-driven per the design.
"""
from __future__ import annotations
import os
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
import sys, os
sys.path.insert(0, os.path.join(_ROOT, "customers/mirion")

import contract, synthetic
from databricks.sdk import WorkspaceClient

CATALOG = "serverless_stable_hvhhmh_catalog"
SCHEMA = "mirion_precall"
WAREHOUSE = "0e8908a6bd79447c"

w = WorkspaceClient(profile="fevm-hvhhmh")

def run(sql: str):
    r = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE, catalog=CATALOG, schema=SCHEMA, statement=sql, wait_timeout="50s"
    )
    st = r.status.state.value if r.status and r.status.state else "?"
    if st != "SUCCEEDED":
        raise SystemExit(f"FAILED [{st}]: {sql[:80]}\n  {getattr(r.status,'error',None)}")
    return r

def sqlstr(v) -> str:
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"

# schema
w.statement_execution.execute_statement(
    warehouse_id=WAREHOUSE, statement=f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}", wait_timeout="50s"
)
print(f"schema {CATALOG}.{SCHEMA} ready")

data = synthetic.generate(seed=0, rows_per_company=2)
for view, cols in contract.VIEWS.items():
    src = f"src_{view}"                      # backing table
    coldefs = ", ".join(f"`{c}` STRING" if c not in ("value", "qty") else f"`{c}` BIGINT" for c in cols)
    run(f"CREATE OR REPLACE TABLE {src} ({coldefs})")
    rows = data[view]
    values = ",\n".join("(" + ", ".join(sqlstr(r[c]) for c in cols) + ")" for r in rows)
    run(f"INSERT INTO {src} ({', '.join('`'+c+'`' for c in cols)}) VALUES\n{values}")
    run(f"CREATE OR REPLACE VIEW {view} ({', '.join('`'+c+'`' for c in cols)}) AS "
        f"SELECT {', '.join('`'+c+'`' for c in cols)} FROM {src}")
    print(f"  {view}: {len(rows)} rows + view")

print("done — verifying vw_orders for one company:")
r = run("SELECT company, order_id, qty, status FROM vw_orders WHERE company = 'Argonne National Laboratory' LIMIT 3")
for row in (r.result.data_array or []):
    print("   ", row)
