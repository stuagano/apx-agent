"""Tool: fetch a single contract record by ID."""

from __future__ import annotations

from typing import Any

from apx_agent import Dependencies, ResourceSpec, attach_resources
from databricks_tools_core.sql import sql_literal

from config import get_settings
from ._sql import run_sql

Workspace = Dependencies.Client


def summarize_contract(contract_id: str, ws: Workspace = None) -> dict[str, Any]:
    """Fetch the structured record for a single contract by ID.

    contract_id: e.g. 'CT-0001' (returned by query_portfolio).
    """
    s = get_settings()
    table = s.qualified_table("primary")
    sql = (
        f"SELECT * FROM {table} WHERE contract_id = '{sql_literal(contract_id)}' LIMIT 1"
    )
    rows = run_sql(ws, sql)
    if not rows:
        return {"contract": None, "found": False, "contract_id": contract_id}
    return {"contract": rows[0], "found": True}


_settings = get_settings()
if _settings.catalog and _settings.schema:
    attach_resources(
        summarize_contract,
        [ResourceSpec("uc_table", _settings.qualified_table("primary"))],
    )
