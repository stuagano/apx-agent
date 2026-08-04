"""Tool: parse application documents from UC Volume PDFs via Claude vision.

Reads document metadata from the catalog, downloads each PDF from a UC Volume,
renders the first page, and extracts structured fields via multimodal FM API.

The vision call is intentional nested LLM work: structured field extraction
from a PDF page cannot move into the outer agent loop without losing the image
payload. It is bounded (max_tokens=600, first page only) and declares the
serving endpoint via attach_resources so log_agent manifests it.
"""
from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from typing import Any

from apx_agent import Dependencies, ResourceSpec, ToolError, attach_resources
from databricks.sdk.service.sql import StatementParameterListItem

from config import get_settings

_VISION_ENDPOINT = "databricks-claude-sonnet-4-6"

_PROMPTS = {
    "paystub": (
        "Extract paystub fields. Return JSON with keys: "
        "employee_name, employer, pay_period_end, gross_pay, net_pay, ytd_gross. "
        "Numeric fields: decimals only, no currency symbols."
    ),
    "w2": (
        "Extract W-2 fields. Return JSON with keys: "
        "employee_name, employer, tax_year, annual_wages, federal_tax_withheld. "
        "Numeric fields: decimals only."
    ),
    "residency": (
        "Extract residency document fields. Return JSON with keys: "
        "tenant_name, address_line, city_state_zip, document_date (ISO format), document_type."
    ),
    "enrollment_letter": (
        "Extract enrollment letter fields. Return JSON with keys: "
        "school_name, student_name, grade_level (int, K=0), academic_year."
    ),
}


def _warehouse_id(ws: Any) -> str:
    serverless, others = [], []
    for wh in ws.warehouses.list():
        if not wh.state or wh.state.value != "RUNNING":
            continue
        (serverless if wh.warehouse_type and "serverless" in str(wh.warehouse_type).lower() else others).append(wh)
    pool = serverless or others
    if not pool:
        raise ToolError("No running SQL warehouse found")
    return pool[0].id


def _list_documents(application_id: str, ws: Any) -> list[dict[str, Any]]:
    s = get_settings()
    result = ws.statement_execution.execute_statement(
        warehouse_id=_warehouse_id(ws),
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


def _extract_via_vision(volume_path: str, doc_type: str, ws: Any) -> dict[str, Any]:
    """Render PDF first page → PNG → Claude vision → structured JSON."""
    from pdf2image import convert_from_bytes

    pdf_bytes = ws.files.download(volume_path).contents.read()
    images = convert_from_bytes(pdf_bytes, dpi=200, first_page=1, last_page=1)
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        images[0].save(tmp.name, "PNG")
        png_b64 = base64.standard_b64encode(Path(tmp.name).read_bytes()).decode("ascii")

    prompt = _PROMPTS.get(doc_type, "Extract all visible fields and return JSON.")
    response = ws.api_client.do(
        method="POST",
        path=f"/serving-endpoints/{_VISION_ENDPOINT}/invocations",
        body={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}},
                    ],
                }
            ],
            "max_tokens": 600,
        },
        headers={"Content-Type": "application/json"},
    )
    raw = response["choices"][0]["message"]["content"]
    if isinstance(raw, list):
        raw = "".join(p.get("text", "") for p in raw if isinstance(p, dict))
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def parse_documents(application_id: str, ws: Dependencies.Workspace) -> dict[str, Any]:
    """Parse all documents for an application via Claude vision.

    Invokes a bounded multimodal serving-endpoint call per PDF (first page,
    max_tokens=600). Nested relative to the outer agent loop by necessity —
    the image payload cannot round-trip through tool text. Declares the
    endpoint and documents table via attach_resources for log_agent.

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
    return {
        "application_id": application_id,
        "documents": [
            {
                "document_id": d["document_id"],
                "document_type": d["document_type"],
                "extracted": _extract_via_vision(d["volume_path"], d["document_type"], ws),
                "confidence_concern": d["ocr_quality_hint"] in ("fair", "poor"),
            }
            for d in docs
        ],
    }


_parse_specs = [ResourceSpec("serving_endpoint", _VISION_ENDPOINT)]
_s = get_settings()
_parse_specs.append(ResourceSpec("uc_table", _s.table("documents")))
attach_resources(parse_documents, _parse_specs)
