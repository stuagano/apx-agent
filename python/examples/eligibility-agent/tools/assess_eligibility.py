"""Tool: apply income-based eligibility logic.

Uses Federal Poverty Level (FPL) thresholds to determine eligibility tier:
  - ≤ 185% FPL → eligible, priority tier
  - ≤ 400% FPL → eligible, standard tier
  - > 400% FPL → ineligible

FPL thresholds are the US HHS 2025 contiguous-states values, encoded inline
for demo determinism. A production deployment should read from an authoritative
benefits-rule service or configuration table.
"""
from __future__ import annotations

from typing import Any

_FPL_BASE = 15_650
_FPL_PER_ADDITIONAL_PERSON = 5_500


def _fpl_100(household_size: int) -> int:
    return _FPL_BASE + _FPL_PER_ADDITIONAL_PERSON * (max(household_size, 1) - 1)


def assess_eligibility(
    application_id: str,
    annual_household_income: float,
    household_size: int,
    residency_result: dict[str, Any],
) -> dict[str, Any]:
    """Determine eligibility tier from income, household size, and residency.

    Args:
        application_id: ID of the application being assessed
        annual_household_income: total annual income from compute_income
        household_size: number of household members from get_household
        residency_result: output of check_residency

    Returns:
        decision: "eligible" | "ineligible" | "blocked"
        tier: "priority" | "standard" | None
        fpl_thresholds: thresholds used for this household size
        income_to_fpl_ratio: income / 100% FPL
        blockers: list of blocking issues (non-empty when decision == "blocked")
    """
    blockers: list[str] = []
    if not residency_result.get("verified"):
        blockers.append("residency not verified: " + "; ".join(residency_result.get("issues", [])))

    fpl_100 = _fpl_100(household_size)
    fpl_185 = int(round(fpl_100 * 1.85))
    fpl_400 = int(round(fpl_100 * 4.00))
    ratio = annual_household_income / fpl_100 if fpl_100 else 0

    if blockers:
        decision, tier = "blocked", None
    elif annual_household_income <= fpl_185:
        decision, tier = "eligible", "priority"
    elif annual_household_income <= fpl_400:
        decision, tier = "eligible", "standard"
    else:
        decision, tier = "ineligible", None

    return {
        "application_id": application_id,
        "decision": decision,
        "tier": tier,
        "annual_household_income": round(annual_household_income, 2),
        "household_size": household_size,
        "fpl_thresholds": {"100_pct": fpl_100, "185_pct": fpl_185, "400_pct": fpl_400},
        "income_to_fpl_ratio": round(ratio, 4),
        "blockers": blockers,
    }
