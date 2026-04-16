"""Explain My Bill agent tools.

Each function becomes a tool: type hints define the schema, the docstring
is the description. Dependencies.* parameters are injected by FastAPI and
excluded from the tool schema.

Queries Unity Catalog tables via the Statement Execution API. Swap DEMO_CATALOG
and DEMO_SCHEMA to point at your own data.

Governance story
────────────────
Every request to a Databricks App carries Databricks-injected headers:

  X-Forwarded-Email              → user's email (from SSO)
  X-Forwarded-Preferred-Username → display name
  X-Forwarded-Access-Token       → short-lived OBO token scoped to this user

get_session_context() is called first on every interaction to show who is
querying and what governance controls are in effect.
"""

from __future__ import annotations

import os
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem, StatementState

from .core import Dependencies
from .core.agent import Agent

Client = Dependencies.Client
Headers = Dependencies.Headers

# ---------------------------------------------------------------------------
# Config — override via environment variables
# ---------------------------------------------------------------------------

CATALOG = os.environ.get("DEMO_CATALOG", "my_catalog")
SCHEMA = os.environ.get("DEMO_SCHEMA", "billing")
WAREHOUSE_ID = os.environ.get("WAREHOUSE_ID", "")

# Expected tables: customers, ami_hourly_rollups, billing_history, rate_schedules


def _param(name: str, value: Any) -> StatementParameterListItem:
    """Shorthand to build a Statement Execution query parameter."""
    return StatementParameterListItem(name=name, value=str(value))


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def _identity(headers: Dependencies.Headers) -> tuple[str, str]:
    """Return (email, auth_method) from injected Databricks Apps headers."""
    if headers and headers.user_email:
        method = "OBO (X-Forwarded-Access-Token)" if headers.token else "Databricks Apps SSO"
        return headers.user_email, method
    try:
        ws = WorkspaceClient()
        me = ws.current_user.me()
        return me.user_name or "local-dev", "Databricks CLI credential"
    except Exception:
        return "local-dev", "local"


def get_session_context(headers: Headers) -> dict:
    """Return the current user's identity and data access context.

    Call this first to establish who is querying, how they authenticated,
    and what governance controls are in effect for this session.
    """
    email, auth_method = _identity(headers)
    obo_active = bool(headers and headers.token)

    return {
        "user": email,
        "auth_method": auth_method,
        "obo_token": obo_active,
        "catalog": f"{CATALOG}.{SCHEMA}",
        "tables": ["customers", "ami_hourly_rollups", "billing_history", "rate_schedules"],
    }


# ---------------------------------------------------------------------------
# SQL helper
# ---------------------------------------------------------------------------

def _run_sql(
    ws: WorkspaceClient,
    sql: str,
    params: list[StatementParameterListItem] | None = None,
) -> list[dict[str, Any]]:
    """Execute parameterized SQL via Statement Execution API and return list of row dicts."""
    response = ws.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID,
        statement=sql,
        parameters=params,
        catalog=CATALOG,
        schema=SCHEMA,
    )
    if response.status and response.status.state != StatementState.SUCCEEDED:
        error_msg = ""
        if response.status.error:
            error_msg = response.status.error.message or str(response.status.error)
        return [{"error": f"SQL execution failed: {error_msg}"}]
    if not response.result or not response.result.data_array:
        return []

    columns = [c.name for c in response.manifest.schema.columns]
    rows = []
    for row_data in response.result.data_array:
        rows.append({col: val for col, val in zip(columns, row_data)})
    return rows


def _cast_numerics(row: dict) -> dict:
    """Best-effort cast stringified numbers back to float."""
    out = {}
    for k, v in row.items():
        if v is None:
            out[k] = None
        else:
            try:
                out[k] = float(v)
            except (ValueError, TypeError):
                out[k] = v
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def get_customer_profile(ws: Client, customer_id: str = "", name: str = "") -> dict[str, Any]:
    """Look up a customer's account profile by ID or name.
    Returns address, rate plan, account status, and linked AMI device.
    Provide either customer_id (e.g. CUST-0001) or a partial name to search."""
    if not customer_id and not name:
        return {"error": "Provide either customer_id or name"}

    if customer_id:
        sql = "SELECT * FROM customers WHERE customer_id = :customer_id"
        params = [_param("customer_id", customer_id)]
    else:
        sql = "SELECT * FROM customers WHERE LOWER(name) LIKE LOWER(:pattern) LIMIT 1"
        params = [_param("pattern", f"%{name}%")]

    rows = _run_sql(ws, sql, params)
    if not rows:
        return {"error": "Customer not found"}
    if "error" in rows[0]:
        return rows[0]
    return rows[0]


def query_ami_readings(customer_id: str, start_date: str, end_date: str, ws: Client) -> dict[str, Any]:
    """Get daily energy usage readings for a customer from AMI smart meter data.
    Returns daily kWh totals with min/max/avg breakdowns.
    customer_id: e.g. CUST-0001
    start_date / end_date: YYYYMMDD format (e.g. 20250318)"""

    sd = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
    ed = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"

    sql = """
    SELECT read_date, daily_kwh, min_kwh, max_kwh, avg_kwh, readings
    FROM ami_hourly_rollups
    WHERE customer_id = :customer_id
      AND read_date BETWEEN :start_date AND :end_date
    ORDER BY read_date
    """
    params = [
        _param("customer_id", customer_id),
        _param("start_date", sd),
        _param("end_date", ed),
    ]
    rows = _run_sql(ws, sql, params)
    if rows and "error" in rows[0]:
        return rows[0]
    return {
        "customer_id": customer_id,
        "start_date": start_date,
        "end_date": end_date,
        "days": len(rows),
        "readings": [_cast_numerics(r) for r in rows],
    }


def get_billing_summary(customer_id: str, months: int, ws: Client) -> dict[str, Any]:
    """Get a customer's recent billing history with full tier breakdown
    (kWh and charges per tier), taxes, and payment status.
    customer_id: e.g. CUST-0001
    months: number of recent months to return (use 3 if unsure)"""

    sql = """
    SELECT * FROM billing_history
    WHERE customer_id = :customer_id
    ORDER BY billing_month DESC
    LIMIT :limit
    """
    params = [
        _param("customer_id", customer_id),
        _param("limit", int(months)),
    ]
    rows = _run_sql(ws, sql, params)
    if rows and "error" in rows[0]:
        return rows[0]
    if not rows:
        return {"error": f"No billing history for {customer_id}"}
    return {
        "customer_id": customer_id,
        "months_returned": len(rows),
        "bills": [_cast_numerics(r) for r in rows],
    }


def get_rate_schedule(rate_plan_id: str, ws: Client) -> dict[str, Any]:
    """Get the details of a rate plan including tier thresholds (kWh limits),
    per-kWh rates for each tier, fixed monthly charges, and demand charges.
    rate_plan_id: e.g. RP-BASIC, RP-TOU, RP-GREEN, RP-EV, RP-COMM"""

    sql = "SELECT * FROM rate_schedules WHERE rate_plan_id = :rate_plan_id"
    params = [_param("rate_plan_id", rate_plan_id)]
    rows = _run_sql(ws, sql, params)
    if not rows:
        return {"error": f"Rate plan {rate_plan_id} not found"}
    if "error" in rows[0]:
        return rows[0]
    return _cast_numerics(rows[0])


def compare_months(customer_id: str, month1: str, month2: str, ws: Client) -> dict[str, Any]:
    """Compare a customer's energy usage and billing between two months.
    Shows side-by-side billing breakdown and AMI data, plus computed deltas
    (kWh change, cost change, percentage changes).
    customer_id: e.g. CUST-0001
    month1 / month2: YYYYMM format (e.g. 202503)"""

    bill_sql = """
    SELECT * FROM billing_history
    WHERE customer_id = :customer_id AND billing_month IN (:month1, :month2)
    ORDER BY billing_month
    """
    bill_params = [
        _param("customer_id", customer_id),
        _param("month1", month1),
        _param("month2", month2),
    ]
    bills = _run_sql(ws, bill_sql, bill_params)
    if bills and "error" in bills[0]:
        return bills[0]
    if not bills:
        return {"error": f"No billing data for {customer_id} in those months"}

    result: dict[str, Any] = {"customer_id": customer_id, "months": {}}

    for b in bills:
        bill = _cast_numerics(b)
        month = b["billing_month"]

        start = f"{month[:4]}-{month[4:]}-01"
        end = f"{month[:4]}-{month[4:]}-31"
        ami_sql = """
        SELECT
            COUNT(DISTINCT read_date) AS days_with_data,
            SUM(daily_kwh)            AS total_kwh,
            AVG(daily_kwh)            AS avg_daily_kwh,
            MAX(max_kwh)              AS peak_kwh
        FROM ami_hourly_rollups
        WHERE customer_id = :customer_id
          AND read_date BETWEEN :start_date AND :end_date
        """
        ami_params = [
            _param("customer_id", customer_id),
            _param("start_date", start),
            _param("end_date", end),
        ]
        ami_rows = _run_sql(ws, ami_sql, ami_params)
        ami = _cast_numerics(ami_rows[0]) if ami_rows and "error" not in ami_rows[0] else {}

        result["months"][month] = {"billing": bill, "ami": ami}

    if month1 in result["months"] and month2 in result["months"]:
        b1 = result["months"][month1]["billing"]
        b2 = result["months"][month2]["billing"]
        kwh1 = b1.get("total_kwh", 0) or 0
        kwh2 = b2.get("total_kwh", 0) or 0
        cost1 = b1.get("amount_due", 0) or 0
        cost2 = b2.get("amount_due", 0) or 0
        result["comparison"] = {
            "kwh_change": round(kwh2 - kwh1, 2),
            "kwh_change_pct": round(((kwh2 - kwh1) / max(kwh1, 1)) * 100, 1),
            "cost_change": round(cost2 - cost1, 2),
            "cost_change_pct": round(((cost2 - cost1) / max(cost1, 1)) * 100, 1),
        }

    return result


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

agent = Agent(tools=[
    get_session_context,
    get_customer_profile,
    query_ami_readings,
    get_billing_summary,
    get_rate_schedule,
    compare_months,
])
