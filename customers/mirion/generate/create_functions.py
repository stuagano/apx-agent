"""Create 7 scalar UC functions (one per brief section) on fevm-hvhhmh.

Each `<section>(company STRING) RETURNS STRING` returns that section's rows for the
company as a JSON array (to_json(collect_list(struct(...)))). Scalar so
uc_function_toolkit surfaces it as a tool; the rich COMMENT becomes the
LLM-facing tool description (the semantic lever for tool selection).
"""
from __future__ import annotations
import os
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
import sys
sys.path.insert(0, os.path.join(_ROOT, "customers/mirion")
import contract
from databricks.sdk import WorkspaceClient

CATALOG, SCHEMA, WAREHOUSE = "serverless_stable_hvhhmh_catalog", "mirion_precall", "0e8908a6bd79447c"
w = WorkspaceClient(profile="fevm-hvhhmh")

# section function name -> (backing view, rich COMMENT). The COMMENT is what the
# agent reads when deciding which tool to call — dense, domain-specific, keyworded.
FUNCS = {
    "open_orders_and_shipping": ("vw_orders",
        "Open manufacturing orders and shipment status for a Mirion customer, from SAP. "
        "Returns a JSON array of the company's in-flight sales orders: order id, product "
        "description (detectors, dosimeters, survey meters, spectrometers), quantity, "
        "expected ship date, and current fulfillment status (Open, In Progress, Blocked, "
        "Closed). Call this when preparing a pre-call brief to see what hardware the "
        "customer has on order and whether any shipments are delayed or blocked."),
    "open_opportunities": ("vw_opportunities",
        "Open sales opportunities (pipeline) for a Mirion customer, from Salesforce. "
        "Returns a JSON array of opportunities with name, sales stage (Discovery, "
        "Qualification, Proposal, Negotiation, Closed Won), dollar value, and close date. "
        "Call this to understand the active deals, deal size, and where each stands in the "
        "sales cycle before a customer call."),
    "recent_win_loss": ("vw_winloss",
        "Recent won and lost deals for a Mirion customer, from Salesforce. Returns a JSON "
        "array of outcomes (Won or Lost), the product line involved, and the date. Call "
        "this to see the customer's recent buying history and any competitive losses that "
        "should inform the conversation."),
    "open_rmas": ("vw_rmas",
        "Open RMAs (Return Merchandise Authorizations) for a Mirion customer, from "
        "ServiceMax. Returns a JSON array of active returns/repairs: RMA id, the affected "
        "unit/product, status (Open, In Progress, Blocked, Closed), and date. Call this to "
        "surface open service issues, returned or failed instruments, and repair status "
        "the rep should be aware of before the visit."),
    "open_pprs": ("vw_pprs",
        "Open PPRs (Product Problem Reports / quality issues, CAPA-like) for a Mirion "
        "customer, from SharePoint. Returns a JSON array of quality issues with PPR id, "
        "description, severity (Low, Medium, High, Critical), and status. Call this to see "
        "unresolved product-quality problems affecting the customer — critical context for "
        "a regulated radiation-detection customer before a call."),
    "field_notes": ("vw_field_notes",
        "Field notes and supply-chain handoff notes for a Mirion customer, from Salesforce. "
        "Returns a JSON array of free-text site-visit and handoff notes with the note, "
        "author, and date. Call this to read the latest human context from the field — "
        "expressed interest, site conditions, relationship notes — before the call."),
    "overdue_actions": ("vw_actions",
        "Overdue and upcoming action items for a Mirion customer, from Salesforce. Returns "
        "a JSON array of committed follow-ups (quotes to send, calls to make) with the "
        "action, due date, and status. Call this to catch anything the account team "
        "promised the customer that is overdue or coming due, so nothing slips."),
}


def run(sql, catalog=None, schema=None):
    r = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE, catalog=catalog, schema=schema, statement=sql, wait_timeout="50s")
    st = r.status.state.value if r.status and r.status.state else "?"
    if st != "SUCCEEDED":
        raise SystemExit(f"FAILED [{st}]: {sql[:90]}\n  {getattr(r.status,'error',None)}")
    return r

for fn, (view, comment) in FUNCS.items():
    cols = contract.VIEWS[view]
    struct_cols = ", ".join(f"`{c}`" for c in cols)
    esc_comment = comment.replace("'", "''")
    ddl = (
        f"CREATE OR REPLACE FUNCTION {CATALOG}.{SCHEMA}.{fn}(company STRING)\n"
        f"RETURNS STRING\n"
        f"COMMENT '{esc_comment}'\n"
        f"RETURN (SELECT to_json(collect_list(struct({struct_cols}))) "
        f"FROM {CATALOG}.{SCHEMA}.{view} WHERE company = {fn}.company)"
    )
    run(ddl)
    print(f"  created {fn}(company) -> {view}")

print("\nverify: open_rmas('Palo Verde Nuclear Station')")
r = run(f"SELECT {CATALOG}.{SCHEMA}.open_rmas('Palo Verde Nuclear Station')")
print("   ", (r.result.data_array or [["<none>"]])[0][0][:300])
