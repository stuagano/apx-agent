"""Tool: aggregate annual household income from parsed documents.

Logic:
- Group documents by employee_name.
- Per employee: prefer W-2 annual_wages; else annualize most recent paystub × 26.
- Sum across all employees.
- Flag discrepancies >5% between paystub-annualized and W-2 for the same employee.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

_DISCREPANCY_THRESHOLD = 0.05


def compute_income(parsed: dict[str, Any]) -> dict[str, Any]:
    """Aggregate annual household income from parsed paystub and W-2 documents.

    Returns:
        annual_household_income: float
        source_breakdown: dict[name -> {preferred_source, preferred_amount, w2_amount, paystub_annualized}]
        discrepancies: list of human-readable flag strings
        confidence: "high" | "medium"
    """
    by_employee: dict[str, dict[str, Any]] = defaultdict(dict)
    for doc in parsed.get("documents", []):
        ext = doc.get("extracted", {})
        name = ext.get("employee_name")
        if not name:
            continue
        if doc["document_type"] == "w2":
            by_employee[name]["w2"] = float(ext.get("annual_wages", 0.0))
        elif doc["document_type"] == "paystub":
            by_employee[name]["paystub_gross"] = float(ext.get("gross_pay", 0.0))

    breakdown: dict[str, dict[str, Any]] = {}
    discrepancies: list[str] = []
    total = 0.0

    for name, sources in by_employee.items():
        w2 = sources.get("w2")
        ps = sources.get("paystub_gross")
        ps_annual = round(ps * 26, 2) if ps is not None else None

        if w2 is not None:
            preferred, preferred_source = w2, "w2"
            if ps_annual is not None:
                rel_diff = abs(ps_annual - w2) / max(w2, 1e-9)
                if rel_diff > _DISCREPANCY_THRESHOLD:
                    discrepancies.append(
                        f"{name}: paystub annualized ${ps_annual:,.2f} vs W-2 ${w2:,.2f} — discrepancy {rel_diff:.0%}"
                    )
        elif ps_annual is not None:
            preferred, preferred_source = ps_annual, "paystub_annualized"
            discrepancies.append(f"{name}: paystub-only — no W-2 on file")
        else:
            preferred, preferred_source = 0.0, "none"
            discrepancies.append(f"{name}: no income document available")

        breakdown[name] = {
            "preferred_source": preferred_source,
            "preferred_amount": preferred,
            "w2_amount": w2,
            "paystub_annualized": ps_annual,
        }
        total += preferred

    return {
        "annual_household_income": round(total, 2),
        "source_breakdown": breakdown,
        "discrepancies": discrepancies,
        "confidence": "high" if not discrepancies else "medium",
    }
