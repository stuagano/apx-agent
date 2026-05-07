"""Tool: build an audit-grade markdown reasoning trail from prior tool outputs.

Called last in the agent's reasoning chain. Produces a structured document
suitable for compliance review: every input document, every rule applied,
every threshold checked.
"""
from __future__ import annotations

from typing import Any

from ..config import get_settings


def build_reasoning_trail(
    application_id: str,
    parsed: dict[str, Any],
    income: dict[str, Any],
    residency: dict[str, Any],
    eligibility: dict[str, Any],
) -> str:
    """Produce a markdown audit trail for the eligibility assessment.

    Args:
        application_id: application being assessed
        parsed: output of parse_documents
        income: output of compute_income
        residency: output of check_residency
        eligibility: output of assess_eligibility

    Returns:
        Multi-line markdown string suitable for display or storage.
    """
    s = get_settings()
    decision = eligibility["decision"]
    tier = eligibility.get("tier")
    headline = "Eligible" if decision == "eligible" else decision.capitalize()
    if tier:
        headline += f" ({tier} tier)"

    lines: list[str] = [
        f"## Eligibility Assessment — Application #{application_id}",
        f"**Program:** {s.program_name}",
        f"**Decision:** {headline}",
        f"**Confidence:** {income.get('confidence', 'n/a').capitalize()}",
        "",
        "### Documents reviewed",
    ]

    for d in parsed.get("documents", []):
        ext = d["extracted"]
        dt = d["document_type"]
        if dt == "paystub":
            line = f"- Paystub: {ext.get('employee_name', 'n/a')} — gross ${ext.get('gross_pay', 0):,.2f}, YTD ${ext.get('ytd_gross', 0):,.2f}"
        elif dt == "w2":
            line = f"- W-2: {ext.get('employee_name', 'n/a')}, {ext.get('tax_year', 'n/a')} wages ${ext.get('annual_wages', 0):,.2f}"
        elif dt == "residency":
            line = f"- Residency ({ext.get('document_type', 'document')}): {ext.get('address_line', '')}, {ext.get('city_state_zip', '')}, dated {ext.get('document_date', 'n/a')}"
        elif dt == "enrollment_letter":
            line = f"- Enrollment letter: {ext.get('school_name', 'n/a')}, grade {ext.get('grade_level', 'n/a')}"
        else:
            line = f"- {dt}: {ext}"
        if d.get("confidence_concern"):
            line += "  ⚠ low OCR quality"
        lines.append(line)

    lines += [
        "",
        "### Income computation",
    ]
    for name, src in income.get("source_breakdown", {}).items():
        lines.append(f"- {name}: ${src['preferred_amount']:,.2f} (source: {src['preferred_source']})")
    lines.append(f"- **Household annual income: ${income['annual_household_income']:,.2f}**")

    fpl = eligibility["fpl_thresholds"]
    lines += [
        "",
        "### Eligibility check",
        f"- Household size: {eligibility['household_size']}",
        f"- 100% FPL: ${fpl['100_pct']:,}  |  185% FPL: ${fpl['185_pct']:,}  |  400% FPL: ${fpl['400_pct']:,}",
        f"- Income / FPL: {eligibility['income_to_fpl_ratio']:.0%}",
        f"- Threshold: ≤185% FPL = priority tier; ≤400% FPL = standard tier",
        f"- **Result: {headline}**",
    ]

    flags = list(income.get("discrepancies", [])) + list(eligibility.get("blockers", []))
    if flags:
        lines += ["", "### Flags"]
        lines.extend(f"- {f}" for f in flags)

    lines += [
        "",
        "---",
        "*Income thresholds use 2025 HHS Federal Poverty Level guidelines for the contiguous states. "
        "A production deployment should read thresholds from an authoritative benefits-rule service.*",
    ]

    return "\n".join(lines)
