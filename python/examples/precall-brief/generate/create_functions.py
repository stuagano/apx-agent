"""Create 7 scalar UC functions (one per brief section) on the workspace.

Each `<section>(company STRING) RETURNS STRING` returns that section's rows for the
company as a JSON array (to_json(collect_list(struct(...)))). Scalar so
uc_function_toolkit surfaces it as a tool; the rich COMMENT becomes the
LLM-facing tool description (the semantic lever for tool selection).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Fix sys.path: parent is examples/precall-brief
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)

import contract
from databricks.sdk import WorkspaceClient


# Section function name -> (backing view, rich COMMENT). The COMMENT is what the
# agent reads when deciding which tool to call — dense, domain-specific, keyworded.
FUNCS = {
    "open_orders_and_shipping": (
        "vw_orders",
        "In-flight orders and shipment status from your ERP. "
        "Returns a JSON array of the company's active orders: order id, product "
        "description, quantity, expected ship date, and fulfillment status (Open, In Progress, Blocked, "
        "Closed). Call this to see what products are on order and whether any shipments are delayed.",
    ),
    "open_opportunities": (
        "vw_opportunities",
        "Open sales pipeline from your CRM. "
        "Returns a JSON array of opportunities with deal name, sales stage (Discovery, "
        "Qualification, Proposal, Negotiation, Closed Won), dollar value, and close date. "
        "Call this to understand active deals, deal size, and sales-cycle status before a customer call.",
    ),
    "recent_win_loss": (
        "vw_winloss",
        "Recent won and lost deals from your CRM. Returns a JSON "
        "array of outcomes (Won or Lost), the product involved, and the date. Call "
        "this to see the customer's recent buying history and any competitive losses.",
    ),
    "open_rmas": (
        "vw_rmas",
        "Open RMAs (Return Merchandise Authorizations) from your field-service system. "
        "Returns a JSON array of active returns/repairs: RMA id, affected "
        "unit/product, status (Open, In Progress, Blocked, Closed), and date. Call this to "
        "surface open service issues and repair status before the visit.",
    ),
    "open_pprs": (
        "vw_pprs",
        "Open product-quality issues from your document store. Returns a JSON array of "
        "quality issues with id, description, severity (Low, Medium, High, Critical), and status. "
        "Call this to see unresolved product problems affecting the customer.",
    ),
    "field_notes": (
        "vw_field_notes",
        "Field notes and supply-chain handoff notes from your CRM. "
        "Returns a JSON array of free-text site-visit and handoff notes with the note, "
        "author, and date. Call this to read the latest human context from the field — "
        "expressed interest, site conditions, relationship notes — before the call.",
    ),
    "overdue_actions": (
        "vw_actions",
        "Committed follow-up actions from your CRM. Returns "
        "a JSON array of committed follow-ups (quotes to send, calls to make) with the "
        "action, due date, and status. Call this to catch anything promised that is overdue.",
    ),
}


def run(
    sql: str, w: WorkspaceClient, warehouse_id: str, catalog: str, schema: str
) -> None:
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
            f"FAILED [{st}]: {sql[:90]}\n  {getattr(r.status, 'error', None)}"
        )
    return r


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create 7 pre-call brief UC functions"
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

    for fn, (view, comment) in FUNCS.items():
        cols = contract.VIEWS[view]
        struct_cols = ", ".join(f"`{c}`" for c in cols)
        esc_comment = comment.replace("'", "''")
        ddl = (
            f"CREATE OR REPLACE FUNCTION {args.catalog}.{args.schema}.{fn}(company STRING)\n"
            f"RETURNS STRING\n"
            f"COMMENT '{esc_comment}'\n"
            f"RETURN (SELECT to_json(collect_list(struct({struct_cols}))) "
            f"FROM {args.catalog}.{args.schema}.{view} WHERE company = {fn}.company)"
        )
        run(ddl, w, args.warehouse_id, args.catalog, args.schema)
        print(f"  created {fn}(company) -> {view}")

    print(f"\nverify: open_rmas('{contract.COMPANIES[0]}')")
    r = run(
        f"SELECT {args.catalog}.{args.schema}.open_rmas('{contract.COMPANIES[0]}')",
        w,
        args.warehouse_id,
        args.catalog,
        args.schema,
    )
    print("   ", (r.result.data_array or [["<none>"]])[0][0][:300])


if __name__ == "__main__":
    main()
