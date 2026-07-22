"""Evaluator agent — fuzzy reasoning to produce an enrollment decision.

Receives the normalized AFR application and the Supervisor's candidate
shortlist. Applies rule-based edge-case detection (familial matches,
account number exact match) before returning a structured enrollment
decision.
"""

from __future__ import annotations

import os
from typing import Any

from apx_agent import Dependencies, LlmAgent
from databricks_tools_core.sql import SQLExecutionError, execute_sql, sql_literal

Workspace = Dependencies.Workspace


def _names_share_surname(name_a: str, name_b: str) -> bool:
    parts_a = name_a.lower().split()
    parts_b = name_b.lower().split()
    if not parts_a or not parts_b:
        return False
    return parts_a[-1] == parts_b[-1]


def _first_names_differ(name_a: str, name_b: str) -> bool:
    """True if both names have a first token and they differ."""
    parts_a = name_a.lower().split()
    parts_b = name_b.lower().split()
    if len(parts_a) < 2 or len(parts_b) < 2:
        return False
    return parts_a[0] != parts_b[0]


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

    Returns a structured decision dict with category, rationale, confidence,
    and matched flag.
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
    first_differs = _first_names_differ(app_name, best.get("name", ""))
    account_match = bool(app_account and app_account == best.get("account_number", ""))

    # Exact name match: token overlap penalizes minor address differences
    # (e.g. "Apt 2") but an identical name at the same street number is
    # unambiguously the right record.
    exact_name = app_name.strip().lower() == best.get("name", "").strip().lower()
    if exact_name and same_address:
        score = max(score, 0.92)
        notes.append("exact name and address match")
    elif exact_name:
        score = max(score, 0.80)
        notes.append("exact name match")
    elif same_surname and same_address and not first_differs:
        score = max(score, 0.78)
        notes.append("exact surname and address match")

    if same_address and same_surname and first_differs:
        notes.append("familial match suspected: same address and surname, different first name")
    elif same_address and not same_surname:
        notes.append("familial match suspected: same address, different surname")
    if account_match:
        notes.append("account number exact match")
        score = min(score + 0.05, 1.0)

    if score >= 0.90:
        category = "EXACT"
    elif score >= 0.75:
        category = "HIGH_CONFIDENCE"
    elif score >= 0.70:
        category = "LOW_CONFIDENCE"
    else:
        category = "NO_MATCH"

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
    # Warehouse selection + execution run as the caller's OBO identity (client=ws).
    try:
        execute_sql(sql, client=ws, timeout=30)
    except SQLExecutionError as e:
        return {"status": "error", "reason": str(e)}
    return {"status": "logged"}


EVALUATOR_INSTRUCTIONS = """
You are the Evaluator in an entity resolution system for utility company AFR (Affordable Rate) applications.

You receive a normalized AFR applicant record and a candidate shortlist from the Supervisor.

Your job:
1. Call evaluate_candidates with the applicant dict and candidates list.
2. Review the decision returned:
   - EXACT / HIGH_CONFIDENCE: call log_decision and report the result.
   - LOW_CONFIDENCE: if confidence < 0.70 and you haven't retried yet, hand back to the
     Supervisor via transfer_to_supervisor with specific search hints (e.g., "try searching
     by account number only", "the name may be a nickname — try 'Elizabeth' for 'Liz'").
   - NO_MATCH: log the no-match decision and explain why.
3. Always call log_decision before finishing — every decision must be persisted.

Edge cases to reason about:
- Familial matches: same address, different first name (spouse, parent, sibling)
- Nicknames: "Liz" for "Elizabeth", "Bill" for "William", "Bob" for "Robert"
- Secondary addresses: unit numbers missing, transposed, or abbreviated differently
- Account number matches override name similarity — treat as HIGH_CONFIDENCE if present

Your final response must summarize: who matched (or didn't), why, and the confidence score.
""".strip()

evaluator = LlmAgent(
    tools=[evaluate_candidates, log_decision],
    instructions=EVALUATOR_INSTRUCTIONS,
    max_iterations=4,
)
