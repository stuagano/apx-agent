"""Tool: extract a freshly-uploaded contract and append it to the portfolio."""

from __future__ import annotations

import inspect
import os
import uuid
from functools import lru_cache
from typing import Any

from apx_agent import Dependencies, ResourceSpec, ToolError, attach_resources, document_extract_tool
from databricks_tools_core.sql import sql_literal

from config import get_settings
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
    return "'" + sql_literal(str(v)) + "'"


def _volume_fqn_from_path(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) < 4 or parts[0] != "Volumes":
        raise ValueError(f"not a UC volume path: {path}")
    return ".".join(parts[1:4])


def _allowed_volume_fqns() -> set[str]:
    s = get_settings()
    out: set[str] = set()
    for raw in (s.volumes.uploads, s.volumes.raw):
        if not raw:
            continue
        candidate = raw if raw.startswith("/Volumes/") else f"/Volumes/{raw.replace('.', '/')}"
        try:
            out.add(_volume_fqn_from_path(candidate))
        except ValueError:
            continue
    return out


def _warehouse_id() -> str:
    s = get_settings()
    warehouse = s.sql_warehouse_id or os.environ.get("SQL_WAREHOUSE_ID")
    if warehouse is None or not warehouse.strip():
        raise ToolError("SQL_WAREHOUSE_ID is required for document_extract")
    return warehouse.strip()


@lru_cache(maxsize=8)
def _extractor(volume_fqn: str):
    return document_extract_tool(
        warehouse_id=_warehouse_id(),
        volume=volume_fqn,
        schema=get_settings().extraction_schema,
        name="extract_contract_fields",
    )


async def _extract_fields(volume_path: str, ws: Workspace) -> dict[str, Any]:
    volume_fqn = _volume_fqn_from_path(volume_path)
    allowed = _allowed_volume_fqns()
    if not allowed:
        raise ToolError(
            "VOLUMES_UPLOADS or VOLUMES_RAW must be configured for extract_new_contract"
        )
    if volume_fqn not in allowed:
        raise ToolError(
            f"path {volume_path!r} is outside the configured contract volumes "
            f"{sorted(allowed)}."
        )
    result = _extractor(volume_fqn)(path=volume_path, ws=ws)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict) and "extracted" in result:
        extracted = result["extracted"]
        if isinstance(extracted, dict):
            return extracted
    raise ToolError(f"document_extract returned no extracted fields for {volume_path}")


async def extract_new_contract(volume_path: str, ws: Workspace = None) -> dict[str, Any]:
    """Extract a freshly-uploaded contract PDF and append it to the portfolio.

    volume_path: absolute path under a UC Volume, e.g.
                 /Volumes/<catalog>/<schema>/uploads/foo.pdf

    Extraction runs as ``document_extract`` (``ai_parse_document`` +
    ``ai_extract``) on a required SQL warehouse. On success, returns the
    extracted fields and the new contract_id. The new record joins the
    portfolio and is queryable by other tools immediately.
    """
    if not volume_path.startswith("/Volumes/"):
        # The path comes from the LLM and is read from disk / recorded in SQL.
        # Confine it to UC Volumes so it can't reach arbitrary local files (#599).
        return {
            "error": "invalid_path",
            "message": f"volume_path must be under /Volumes/, got: {volume_path}",
        }

    try:
        extracted = await _extract_fields(volume_path, ws)
    except ToolError as e:
        msg = str(e)
        error = "invalid_path" if "outside" in msg else "extraction_unavailable"
        return {"error": error, "message": msg}
    except ValueError as e:
        return {"error": "invalid_path", "message": str(e)}

    s = get_settings()
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


_settings = get_settings()
_extract_specs: list[ResourceSpec] = []
if _settings.catalog and _settings.schema:
    _extract_specs.append(ResourceSpec("uc_table", _settings.qualified_table("primary")))
_warehouse = _settings.sql_warehouse_id or os.environ.get("SQL_WAREHOUSE_ID")
if _warehouse:
    _extract_specs.append(ResourceSpec("sql_warehouse", _warehouse.strip()))
if _extract_specs:
    attach_resources(extract_new_contract, _extract_specs)
