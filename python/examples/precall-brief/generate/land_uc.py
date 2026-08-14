"""Land the pre-call synthetic data + governed views on a workspace.

Creates schema, one source table per view (loaded with synthetic rows), and the
7 governed views over them. Catalog and schema are config-driven per the design.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Fix sys.path: parent is examples/precall-brief, need to import contract & synthetic from there
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)

import contract
import synthetic
from databricks.sdk import WorkspaceClient


def run(sql: str, w: WorkspaceClient, warehouse_id: str, catalog: str, schema: str) -> None:
    r = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        catalog=catalog,
        schema=schema,
        statement=sql,
        wait_timeout="50s",
    )
    st = r.status.state.value if r.status and r.status.state else "?"
    if st != "SUCCEEDED":
        raise SystemExit(
            f"FAILED [{st}]: {sql[:80]}\n  {getattr(r.status, 'error', None)}"
        )
    return r


def sqlstr(v) -> str:
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Land pre-call synthetic data + views to Unity Catalog"
    )
    parser.add_argument(
        "--profile",
        default="DEFAULT",
        help="Databricks CLI profile (default: DEFAULT)",
    )
    parser.add_argument(
        "--catalog",
        default="main",
        help="Catalog to use (default: main)",
    )
    parser.add_argument(
        "--schema",
        default="precall",
        help="Schema to use (default: precall)",
    )
    parser.add_argument(
        "--warehouse-id",
        required=True,
        help="SQL warehouse ID",
    )
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile)

    # Create schema
    w.statement_execution.execute_statement(
        warehouse_id=args.warehouse_id,
        statement=f"CREATE SCHEMA IF NOT EXISTS {args.catalog}.{args.schema}",
        wait_timeout="50s",
    )
    print(f"schema {args.catalog}.{args.schema} ready")

    data = synthetic.generate(seed=0, rows_per_company=2)
    for view, cols in contract.VIEWS.items():
        src = f"src_{view}"  # backing table
        coldefs = ", ".join(
            f"`{c}` STRING"
            if c not in ("value", "qty")
            else f"`{c}` BIGINT"
            for c in cols
        )
        run(
            f"CREATE OR REPLACE TABLE {src} ({coldefs})",
            w,
            args.warehouse_id,
            args.catalog,
            args.schema,
        )
        rows = data[view]
        values = ",\n".join(
            "(" + ", ".join(sqlstr(r[c]) for c in cols) + ")"
            for r in rows
        )
        run(
            f"INSERT INTO {src} ({', '.join('`' + c + '`' for c in cols)}) VALUES\n{values}",
            w,
            args.warehouse_id,
            args.catalog,
            args.schema,
        )
        run(
            f"CREATE OR REPLACE VIEW {view} ({', '.join('`' + c + '`' for c in cols)}) AS "
            f"SELECT {', '.join('`' + c + '`' for c in cols)} FROM {src}",
            w,
            args.warehouse_id,
            args.catalog,
            args.schema,
        )
        print(f"  {view}: {len(rows)} rows + view")

    print("done — verifying vw_orders for one company:")
    r = run(
        f"SELECT company, order_id, qty, status FROM {args.catalog}.{args.schema}.vw_orders WHERE company = '{contract.COMPANIES[0]}' LIMIT 3",
        w,
        args.warehouse_id,
        args.catalog,
        args.schema,
    )
    for row in (r.result.data_array or []):
        print("   ", row)


if __name__ == "__main__":
    main()
