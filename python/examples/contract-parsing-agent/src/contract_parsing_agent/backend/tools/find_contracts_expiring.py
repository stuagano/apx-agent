"""Tool: list contracts expiring in the next N days."""

from __future__ import annotations

from typing import Any

from apx_agent import Dependencies

from ..config import get_settings
from ._sql import run_sql

Workspace = Dependencies.Client


def find_contracts_expiring(within_days: int = 90, ws: Workspace = None) -> dict[str, Any]:
    """List contracts expiring within the next N days, soonest first.

    within_days: window in days from today. Default 90.
    """
    s = get_settings()
    table = s.qualified_table("primary")
    n = max(1, int(within_days))
    sql = (
        f"SELECT contract_id, counterparty, contract_type, expiry_date, "
        f"term_years, pricing_summary, auto_renewal "
        f"FROM {table} "
        f"WHERE expiry_date <= date_add(current_date(), {n}) "
        f"  AND expiry_date >= current_date() "
        f"ORDER BY expiry_date ASC LIMIT 50"
    )
    rows = run_sql(ws, sql)
    return {"rows": rows, "count": len(rows), "within_days": n}
