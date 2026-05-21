"""Tool: extract a freshly-uploaded contract and append it to the portfolio."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from apx_agent import Dependencies

from config import get_settings
from extraction import extract
from ._sql import run_sql

Workspace = Dependencies.Client

_FIELDS = [
    "counterparty", "contract_type", "effective_date", "expiry_date",
    "term_years", "pricing_model", "pricing_summary", "auto_renewal",
    "sla_uptime_pct", "notes",
]


def _quote(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def extract_new_contract(volume_path: str, ws: Workspace = None) -> dict[str, Any]:
    """Extract a freshly-uploaded contract PDF and append it to the portfolio.

    volume_path: absolute path under a UC Volume, e.g.
                 /Volumes/<catalog>/<schema>/uploads/foo.pdf

    On success, returns the extracted fields and the new contract_id. The new
    record joins the portfolio and is queryable by other tools immediately.
    """
    s = get_settings()
    pdf = Path(volume_path)

    try:
        extracted = extract(pdf, s.extraction_schema, s.model, ws)
    except RuntimeError as e:
        msg = str(e)
        return {
            "error": "extraction_unavailable",
            "message": msg.split(":", 1)[-1].strip() if ":" in msg else msg,
        }
    except ValueError as e:
        return {"error": "pdf_unreadable", "message": str(e)}

    contract_id = f"CT-LIVE-{uuid.uuid4().hex[:8].upper()}"
    cols = ["contract_id"] + [f for f in _FIELDS if f in extracted]
    vals = [_quote(contract_id)] + [_quote(extracted[f]) for f in _FIELDS if f in extracted]
    sql = (
        f"INSERT INTO {s.qualified_table('primary')} "
        f"({', '.join(cols)}) VALUES ({', '.join(vals)})"
    )
    try:
        run_sql(ws, sql)
    except Exception as e:
        return {"error": "insert_failed", "message": str(e)}
    return {"contract_id": contract_id, "extracted": extracted}
