"""Tool: look up the household record for an application.

Joins applications -> applicants -> households on application_id.
Returns a flat dict with residence and household-size fields. The agent
passes this dict to check_residency and assess_eligibility so they don't
each re-query the catalog.
"""
from __future__ import annotations

from typing import Any

from apx_agent import Dependencies
from databricks.sdk.service.sql import StatementParameterListItem, StatementState

from ..config import get_settings


def _warehouse_id(ws: Any) -> str:
    serverless, others = [], []
    for wh in ws.warehouses.list():
        if not wh.state or wh.state.value != "RUNNING":
            continue
        (serverless if wh.warehouse_type and "serverless" in str(wh.warehouse_type).lower() else others).append(wh)
    pool = serverless or others
    if not pool:
        raise RuntimeError("No running SQL warehouse found")
    return pool[0].id


def _fetch_row(application_id: str, ws: Any) -> dict[str, Any] | None:
    s = get_settings()
    result = ws.statement_execution.execute_statement(
        warehouse_id=_warehouse_id(ws),
        statement=(
            f"SELECT h.household_id, h.primary_filer_name, h.secondary_filer_name, "
            f"h.household_size, h.residence_address, h.residence_city, "
            f"h.residence_state, h.residence_zip "
            f"FROM {s.table('applications')} a "
            f"JOIN {s.table('applicants')} ap ON a.applicant_id = ap.applicant_id "
            f"JOIN {s.table('households')} h ON ap.household_id = h.household_id "
            f"WHERE a.application_id = :app_id"
        ),
        parameters=[StatementParameterListItem(name="app_id", value=application_id, type="STRING")],
        wait_timeout="30s",
    )
    if not result.status or result.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"household query failed: {result.status.error if result.status else 'unknown'}")
    rows = (result.result.data_array or []) if result.result else []
    if not rows:
        return None
    r = rows[0]
    return {
        "household_id": r[0],
        "primary_filer_name": r[1],
        "secondary_filer_name": r[2],
        "household_size": int(r[3]) if r[3] is not None else None,
        "residence_address": r[4],
        "residence_city": r[5],
        "residence_state": r[6],
        "residence_zip": r[7],
    }


def get_household(application_id: str, ws: Dependencies.Workspace) -> dict[str, Any]:
    """Look up the household record associated with an application.

    Returns a dict with keys: household_id, primary_filer_name,
    secondary_filer_name, household_size, residence_address,
    residence_city, residence_state, residence_zip.
    """
    row = _fetch_row(application_id, ws)
    if row is None:
        raise ValueError(f"no household found for application_id={application_id!r}")
    return row
