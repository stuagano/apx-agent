"""Tool: filter the contract portfolio with structured filters."""

from __future__ import annotations

from typing import Any

from apx_agent import Dependencies, ResourceSpec, attach_resources
from databricks_tools_core.sql import sql_literal

from config import get_settings
from ._sql import run_sql

Workspace = Dependencies.Client


def query_portfolio(
    counterparty: str = "",
    contract_type: str = "",
    pricing_model: str = "",
    auto_renewal: bool | None = None,
    expires_within_days: int | None = None,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Filter the contract portfolio. Returns matching contracts.

    Use this for every portfolio listing — including renewal calendars.
    Pass expires_within_days alone to list contracts that expire in the next
    N days and have not already expired (soonest first). Combine with other
    filters when the question is more specific.

    counterparty: utility / counterparty name (exact match)
    contract_type: one of interconnection, ppa, demand_response, tariff, service
    pricing_model: one of fixed, indexed, tiered, time_of_use
    auto_renewal: true to include only auto-renewing contracts
    expires_within_days: only not-yet-expired contracts expiring in the next N days
    """
    s = get_settings()
    table = s.qualified_table("primary")
    where: list[str] = []
    if counterparty:
        where.append(f"counterparty = '{sql_literal(counterparty)}'")
    if contract_type:
        where.append(f"contract_type = '{sql_literal(contract_type)}'")
    if pricing_model:
        where.append(f"pricing_model = '{sql_literal(pricing_model)}'")
    if auto_renewal is not None:
        where.append(f"auto_renewal = {'true' if auto_renewal else 'false'}")
    if expires_within_days is not None:
        n = max(1, int(expires_within_days))
        where.append(f"expiry_date <= date_add(current_date(), {n})")
        where.append("expiry_date >= current_date()")
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        f"SELECT contract_id, counterparty, contract_type, expiry_date, "
        f"term_years, pricing_model, pricing_summary, auto_renewal "
        f"FROM {table}{where_clause} "
        f"ORDER BY expiry_date ASC LIMIT 50"
    )
    rows = run_sql(ws, sql)
    return {"rows": rows, "count": len(rows)}


_settings = get_settings()
if _settings.catalog and _settings.schema:
    attach_resources(
        query_portfolio,
        [ResourceSpec("uc_table", _settings.qualified_table("primary"))],
    )
