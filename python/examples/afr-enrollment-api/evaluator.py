"""Evaluator — rule-based enrollment decision on a candidate shortlist.

Applies familial-match detection and account-number boosting, then returns
a structured decision with category, rationale, and confidence score.
"""

from __future__ import annotations

import os
from typing import Any

from databricks_tools_core.sql import sql_literal


def _names_share_surname(name_a: str, name_b: str) -> bool:
    parts_a = name_a.lower().split()
    parts_b = name_b.lower().split()
    if not parts_a or not parts_b:
        return False
    return parts_a[-1] == parts_b[-1]


def _addresses_match(addr_a: str, addr_b: str) -> bool:
    """True if first token of address (street number) matches."""
    tok_a = addr_a.strip().split()
    tok_b = addr_b.strip().split()
    return bool(tok_a and tok_b and tok_a[0] == tok_b[0])


def evaluate_candidates(
    applicant: dict[str, Any],
    candidates: list[dict[str, Any]],
    ws: Workspace = None,
) -> dict[str, Any]:
    """Evaluate a list of candidates against the original AFR application.

    Returns a structured decision dict with category, rationale, confidence, and matched flag.
    applicant: normalized applicant record (name, address, account_number, email)
    candidates: list of candidate dicts from supervisor (account_id, name, address, score)"""
    if not candidates:
        return {
            "decision": {
                "matched": False,
                "account_id": None,
                "category": "NO_MATCH",
                "rationale": "No candidates were returned by the search.",
                "confidence": 0.0,
                "candidates_reviewed": 0,
            }
        }

    app_name = applicant.get("name", "")
    app_address = applicant.get("address", "")
    app_account = applicant.get("account_number", "")

    best = candidates[0]
    score = float(best.get("score", 0.0))

    notes: list[str] = []
    same_address = _addresses_match(app_address, best.get("address", ""))
    same_surname = _names_share_surname(app_name, best.get("name", ""))
    account_match = bool(app_account and app_account == best.get("account_number", ""))

    if same_address and not same_surname:
        notes.append("familial match suspected: same address, different surname")
    if account_match:
        notes.append("account number exact match")
        score = min(score + 0.05, 1.0)

    if score >= 0.90:
        category = "EXACT"
    elif score >= 0.75:
        category = "HIGH_CONFIDENCE"
    else:
        category = "LOW_CONFIDENCE"

    rationale_parts = [f"Best candidate '{best['name']}' scored {score:.2f}."]
    if notes:
        rationale_parts.append("; ".join(notes).capitalize() + ".")

    return {
        "decision": {
            "matched": score >= 0.70,
            "account_id": best["account_id"] if score >= 0.70 else None,
            "category": category,
            "rationale": " ".join(rationale_parts),
            "confidence": round(score, 3),
            "candidates_reviewed": len(candidates),
        }
    }


def log_decision(
    decision: dict[str, Any],
    ws: Workspace = None,
) -> dict[str, Any]:
    """Write the enrollment decision to the afr_processing table.

    decision: dict with keys: applicant_name, matched, account_id, category,
              rationale, confidence, candidates_reviewed"""
    if os.environ.get("DEMO_MODE", "").lower() == "true":
        return {"status": "logged (demo mode — no real write)", "decision": decision}

    table = os.environ.get("AFR_DECISION_TABLE", "")
    if not table:
        return {"status": "skipped", "reason": "AFR_DECISION_TABLE not configured"}

    def _get_warehouse_id(workspace: Any) -> str:
        for wh in workspace.warehouses.list():
            if wh.warehouse_type and "serverless" in str(wh.warehouse_type).lower():
                return wh.id or ""
        for wh in workspace.warehouses.list():
            if wh.id:
                return wh.id
        raise RuntimeError("No SQL warehouse available")

    from databricks.sdk.service.sql import StatementState

    sql = f"""
        INSERT INTO {table}
        (applicant_name, matched, account_id, category, rationale, confidence, candidates_reviewed, decision_ts)
        VALUES (
            '{decision.get("applicant_name", "")}',
            {str(decision.get("matched", False)).upper()},
            '{decision.get("account_id") or ""}',
            '{decision.get("category", "")}',
            '{sql_literal(decision.get("rationale", ""))}',
            {decision.get("confidence", 0.0)},
            {decision.get("candidates_reviewed", 0)},
            CURRENT_TIMESTAMP()
        )
    """
    result = ws.statement_execution.execute_statement(
        warehouse_id=_get_warehouse_id(ws),
        statement=sql,
        wait_timeout="30s",
    )
    if result.status is None or result.status.state != StatementState.SUCCEEDED:
        error_msg = result.status.error if result.status else "unknown"
        return {"status": "error", "reason": str(error_msg)}
    return {"status": "logged"}


