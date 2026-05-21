"""Tool: filter the contract portfolio with structured filters."""

from __future__ import annotations

from typing import Any

from apx_agent import Dependencies

from config import get_settings
from ._sql import run_sql

Workspace = Dependencies.Client


def _quote(s: str) -> str:
    return s.replace("'", "''")


def query_portfolio(
    counterparty: str = "",
    contract_type: str = "",
    pricing_model: str = "",
    auto_renewal: bool | None = None,
    expires_within_days: int | None = None,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Filter the contract portfolio. Returns a list of contracts matching the
    provided filters.

    counterparty: utility / counterparty name (exact match)
    contract_type: one of interconnection, ppa, demand_response, tariff, service
    pricing_model: one of fixed, indexed, tiered, time_of_use
    auto_renewal: true to include only auto-renewing contracts
    expires_within_days: only contracts expiring in the next N days
    """
    s = get_settings()
    table = s.qualified_table("primary")
    where: list[str] = []
    if counterparty:
        where.append(f"counterparty = '{_quote(counterparty)}'")
    if contract_type:
        where.append(f"contract_type = '{_quote(contract_type)}'")
    if pricing_model:
        where.append(f"pricing_model = '{_quote(pricing_model)}'")
    if auto_renewal is not None:
        where.append(f"auto_renewal = {'true' if auto_renewal else 'false'}")
    if expires_within_days is not None:
        where.append(
            f"expiry_date <= date_add(current_date(), {int(expires_within_days)})"
        )
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        f"SELECT contract_id, counterparty, contract_type, expiry_date, "
        f"term_years, pricing_model, pricing_summary, auto_renewal "
        f"FROM {table}{where_clause} "
        f"ORDER BY expiry_date ASC LIMIT 50"
    )
    rows = run_sql(ws, sql)
    return {"rows": rows, "count": len(rows)}
