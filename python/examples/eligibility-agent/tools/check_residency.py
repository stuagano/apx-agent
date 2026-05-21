"""Tool: verify residency document against the household record.

Three checks:
1. Address line matches household record (case-insensitive, normalized whitespace).
2. Document date is within the configured recency window (default 60 days).
3. State in city_state_zip matches the configured STATE_CODE.

All three must pass for residency to be verified.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from config import get_settings


def _normalize(s: str) -> str:
    return " ".join(s.lower().split()) if s else ""


def check_residency(parsed: dict[str, Any], household: dict[str, Any]) -> dict[str, Any]:
    """Verify the residency document against the household record.

    Args:
        parsed: output of parse_documents
        household: output of get_household

    Returns:
        verified: bool
        address_match: bool
        state_match: bool
        recency_ok: bool
        issues: list[str]
    """
    s = get_settings()
    residency_doc = next(
        (d for d in parsed.get("documents", []) if d["document_type"] == "residency"),
        None,
    )
    if not residency_doc:
        return {"verified": False, "issues": ["no residency document on file"]}

    ext = residency_doc["extracted"]
    issues: list[str] = []

    doc_addr = _normalize(ext.get("address_line", ""))
    hh_addr = _normalize(household.get("residence_address", ""))
    address_match = doc_addr == hh_addr
    if not address_match:
        issues.append(
            f"address mismatch: doc='{ext.get('address_line')}' vs household='{household.get('residence_address')}'"
        )

    csz = ext.get("city_state_zip", "")
    state_match = f" {s.state_code} " in f" {csz} " or csz.endswith(f" {s.state_code}") or f" {s.state_code}," in csz
    if not state_match:
        issues.append(f"document state is not {s.state_code} (got '{csz}')")

    recency_ok = False
    try:
        doc_date = datetime.fromisoformat(ext.get("document_date", "")).date()
        days_old = (date.today() - doc_date).days
        recency_ok = 0 <= days_old <= s.residency_recency_days
        if not recency_ok:
            issues.append(f"document is {days_old} days old (>{s.residency_recency_days}-day recency window)")
    except (ValueError, TypeError):
        issues.append("document date missing or unparseable")

    return {
        "verified": address_match and state_match and recency_ok,
        "address_match": address_match,
        "state_match": state_match,
        "recency_ok": recency_ok,
        "issues": issues,
    }
