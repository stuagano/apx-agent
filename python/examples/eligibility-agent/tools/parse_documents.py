"""Tool: parse application documents from a UC Volume via document_extract.

Reads document metadata from the catalog, then dispatches each row to a
type-specific ``document_extract_tool`` (``ai_parse_document`` +
``ai_extract``) against the documents volume. One committed schema per
document type. Unknown types raise ``ToolError`` — no vision fallback.
"""
from __future__ import annotations

import inspect
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from apx_agent import Dependencies, ResourceSpec, ToolError, attach_resources, document_extract_tool
from databricks.sdk.service.sql import StatementParameterListItem

from config import get_settings

_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
_DOC_TYPES = ("paystub", "w2", "residency", "enrollment_letter")


def _warehouse_id() -> str:
    warehouse = os.environ.get("SQL_WAREHOUSE_ID")
    if warehouse is None or not warehouse.strip():
        raise ToolError("SQL_WAREHOUSE_ID is required for document_extract")
    return warehouse.strip()


def _documents_volume() -> str:
    s = get_settings()
    return f"{s.catalog}.{s.schema}.documents"


def _load_schema(doc_type: str) -> dict[str, Any]:
    path = _SCHEMA_DIR / f"{doc_type}.json"
    return json.loads(path.read_text())


@lru_cache(maxsize=1)
def _extractors() -> dict[str, Any]:
    warehouse = _warehouse_id()
    volume = _documents_volume()
    return {
        doc_type: document_extract_tool(
            warehouse_id=warehouse,
            volume=volume,
            schema=_load_schema(doc_type),
            name=f"extract_{doc_type}",
        )
        for doc_type in _DOC_TYPES
    }


def _list_documents(application_id: str, ws: Any) -> list[dict[str, Any]]:
    s = get_settings()
    result = ws.statement_execution.execute_statement(
        warehouse_id=_warehouse_id(),
        statement=(
            f"SELECT document_id, document_type, volume_path, ocr_quality_hint "
            f"FROM {s.table('documents')} "
            f"WHERE application_id = :app_id"
        ),
        parameters=[StatementParameterListItem(name="app_id", value=application_id, type="STRING")],
        wait_timeout="30s",
    )
    rows = (result.result.data_array or []) if result.result else []
    return [
        {"document_id": r[0], "document_type": r[1], "volume_path": r[2], "ocr_quality_hint": r[3]}
        for r in rows
    ]


async def _extract_document(volume_path: str, doc_type: str, ws: Any) -> dict[str, Any]:
    if doc_type not in _DOC_TYPES:
        raise ToolError(
            f"unsupported document_type {doc_type!r}; "
            f"expected one of {list(_DOC_TYPES)}"
        )
    tool = _extractors()[doc_type]
    result = tool(path=volume_path, ws=ws)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict) and isinstance(result.get("extracted"), dict):
        return result["extracted"]
    raise ToolError(f"document_extract returned no extracted fields for {volume_path}")


async def parse_documents(application_id: str, ws: Dependencies.Workspace) -> dict[str, Any]:
    """Parse all documents for an application via document_extract.

    Lists the application's files from the documents table, then extracts
    each with ``ai_parse_document`` + ``ai_extract`` on the declared SQL
    warehouse. One schema per document type. Declares the warehouse and
    documents table via attach_resources for log_agent.

    Returns:
        {
            "application_id": str,
            "documents": [
                {"document_id": str, "document_type": str, "extracted": dict, "confidence_concern": bool},
                ...
            ]
        }
    """
    docs = _list_documents(application_id, ws)
    parsed = []
    for d in docs:
        parsed.append(
            {
                "document_id": d["document_id"],
                "document_type": d["document_type"],
                "extracted": await _extract_document(d["volume_path"], d["document_type"], ws),
                "confidence_concern": d["ocr_quality_hint"] in ("fair", "poor"),
            }
        )
    return {"application_id": application_id, "documents": parsed}


_s = get_settings()
_parse_specs = [ResourceSpec("uc_table", _s.table("documents"))]
_warehouse = os.environ.get("SQL_WAREHOUSE_ID")
if _warehouse:
    _parse_specs.append(ResourceSpec("sql_warehouse", _warehouse.strip()))
attach_resources(parse_documents, _parse_specs)
