"""Shared extraction logic.

`extract(pdf_path, schema, model, ws)` is called by both:
  - the setup pipeline (batch) — see scripts/setup_portfolio.py
  - the live tool — see tools/extract_new_contract.py

Knows nothing about Spark, Delta, or the agent. Returns a dict.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


def read_pdf_text(path: Path, min_chars: int = 100) -> str:
    """Extract text from a PDF. Raises if the file looks like a scanned image
    (i.e., yields too little text — we don't OCR in this build)."""
    doc = fitz.open(str(path))
    try:
        text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()
    stripped = text.strip()
    if len(stripped) < min_chars:
        raise ValueError(
            f"PDF yielded too little text ({len(stripped)} chars < {min_chars}). "
            "Looks like a scanned image; OCR is not supported in this build."
        )
    return text


def extract(
    pdf_path: Path,
    schema: dict[str, Any],
    model: str,
    ws: Any,  # databricks.sdk.WorkspaceClient
    *,
    max_retries: int = 2,
) -> dict[str, Any]:
    """Extract structured fields from a contract PDF using the FM API.

    Returns a dict matching `schema`. On partial-failure (some fields fail
    schema validation) returns the full parsed dict plus an
    ``_extraction_warnings`` list. On hard failure raises RuntimeError
    after retries.
    """
    text = read_pdf_text(pdf_path)
    system_prompt = (
        "You extract structured fields from utility contracts. "
        "Return ONLY a JSON object matching the provided schema. "
        "If a field is not present in the document, omit it rather than guessing."
    )
    user_prompt = (
        f"Contract text:\n\n{text}\n\n"
        "Extract the fields described by the schema."
    )

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            # Use raw HTTP — serving_endpoints.query() doesn't accept response_format
            response = ws.api_client.do(
                "POST",
                f"/serving-endpoints/{model}/invocations",
                body={
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "contract_fields", "schema": schema, "strict": False},
                    },
                    "max_tokens": 2000,
                },
            )
            raw = response["choices"][0]["message"]["content"] if response.get("choices") else ""
            parsed = json.loads(raw)
            warnings = _validate_against_schema(parsed, schema)
            if warnings:
                parsed["_extraction_warnings"] = warnings
            return parsed
        except json.JSONDecodeError as e:
            last_err = e
            logger.warning("attempt %d JSON parse failed: %s", attempt, e)
            if attempt < max_retries:
                time.sleep(2**attempt)
        except Exception as e:  # SDK transient or auth
            last_err = e
            logger.warning("attempt %d FM API call failed: %s", attempt, e)
            if attempt < max_retries:
                time.sleep(2**attempt)

    raise RuntimeError(f"extraction_unavailable after {max_retries + 1} attempts: {last_err}")


def _validate_against_schema(parsed: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Cheap per-field validation. Returns list of human-readable warnings,
    empty if all good. We don't fail on warnings — we surface them."""
    warnings: list[str] = []
    required = schema.get("required", [])
    props = schema.get("properties", {})
    for r in required:
        if r not in parsed:
            warnings.append(f"missing required field: {r}")
    for field, value in list(parsed.items()):
        if field.startswith("_"):
            continue
        spec = props.get(field)
        if not spec:
            continue
        if "enum" in spec and value not in spec["enum"]:
            warnings.append(
                f"field '{field}' value '{value}' not in enum {spec['enum']}"
            )
    return warnings
