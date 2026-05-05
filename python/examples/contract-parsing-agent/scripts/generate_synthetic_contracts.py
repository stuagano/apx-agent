"""Generate synthetic utility contracts with known ground truth.

Outputs:
  - <out_dir>/<contract_id>.pdf  (text-PDF, 2-3 pages)
  - <out_dir>/ground_truth.json  (list of dicts, one per contract)

Run from a Databricks notebook (Task 6) or locally for tests.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

# Deterministic by default — same corpus every run.
SEED = 1337

COUNTERPARTIES = [
    "Pacific Gas & Electric", "Southern California Edison", "Con Edison",
    "Duke Energy", "Xcel Energy", "Dominion Energy", "NextEra Energy",
    "Exelon", "PSEG", "Entergy",
]

CONTRACT_TYPES = ["interconnection", "ppa", "demand_response", "tariff", "service"]
PRICING_MODELS = ["fixed", "indexed", "tiered", "time_of_use"]


@dataclass
class GroundTruth:
    contract_id: str
    counterparty: str
    contract_type: str
    effective_date: str
    expiry_date: str
    term_years: float
    pricing_model: str
    pricing_summary: str
    auto_renewal: bool
    sla_uptime_pct: float
    notes: str


def _make_one(idx: int, rng: random.Random, anchor: date) -> GroundTruth:
    counterparty = rng.choice(COUNTERPARTIES)
    ctype = rng.choice(CONTRACT_TYPES)
    term_years = rng.choice([1, 3, 5, 7, 10, 15])
    effective_offset_days = rng.randint(-365 * 4, -30)
    effective = anchor + timedelta(days=effective_offset_days)
    expiry = effective + timedelta(days=int(term_years * 365.25))
    pricing_model = rng.choice(PRICING_MODELS)
    pricing_summary = {
        "fixed": "Flat $0.072/kWh fixed across the term.",
        "indexed": "Indexed to monthly natural-gas index plus 12% premium.",
        "tiered": "Tiered: $0.06 first 200 MWh, $0.085 above.",
        "time_of_use": "On-peak $0.18/kWh; off-peak $0.06/kWh.",
    }[pricing_model]
    return GroundTruth(
        contract_id=f"CT-{idx:04d}",
        counterparty=counterparty,
        contract_type=ctype,
        effective_date=effective.isoformat(),
        expiry_date=expiry.isoformat(),
        term_years=float(term_years),
        pricing_model=pricing_model,
        pricing_summary=pricing_summary,
        auto_renewal=rng.random() < 0.4,
        sla_uptime_pct=rng.choice([99.0, 99.5, 99.9, 99.95]),
        notes=f"Synthetic record #{idx} for chatbot-contracts demo.",
    )


def _render_pdf(path: Path, gt: GroundTruth) -> None:
    doc = SimpleDocTemplate(str(path), pagesize=LETTER)
    styles = getSampleStyleSheet()
    flow = []
    title_map = {
        "interconnection": "INTERCONNECTION AGREEMENT",
        "ppa": "POWER PURCHASE AGREEMENT",
        "demand_response": "DEMAND RESPONSE AGREEMENT",
        "tariff": "RETAIL TARIFF AGREEMENT",
        "service": "SERVICE AGREEMENT",
    }
    flow.append(Paragraph(f"<b>{title_map[gt.contract_type]}</b>", styles["Title"]))
    flow.append(Paragraph(f"Contract ID: {gt.contract_id}", styles["Normal"]))
    flow.append(Spacer(1, 12))
    body = [
        f"<b>Counterparty.</b> This agreement is entered into by Demo Energy Co. and "
        f"{gt.counterparty} (the 'Counterparty').",
    ]
    if gt.contract_type == "ppa":
        body.append("<b>Contract Classification.</b> ppa (Power Purchase Agreement).")
    body += [
        f"<b>Effective Date.</b> {gt.effective_date}.",
        f"<b>Expiration Date.</b> {gt.expiry_date}.",
        f"<b>Initial Term.</b> {gt.term_years:g} years from the Effective Date.",
        f"<b>Pricing Terms.</b> {gt.pricing_summary}",
        f"<b>Pricing Model.</b> {gt.pricing_model}.",
        f"<b>Auto-Renewal.</b> "
        + ("This agreement automatically renews for successive 1-year terms unless either party "
           "provides 90 days' written notice." if gt.auto_renewal
           else "This agreement does not auto-renew. Renewal requires a fully-executed amendment."),
        f"<b>Service Level.</b> Counterparty shall maintain at least {gt.sla_uptime_pct}% uptime "
        "across the contract term, measured monthly.",
        f"<b>Notes.</b> {gt.notes}",
    ]
    for p in body:
        flow.append(Paragraph(p, styles["Normal"]))
        flow.append(Spacer(1, 8))
    doc.build(flow)


def generate(out_dir: Path, n: int = 18, seed: int = SEED) -> list[dict]:
    """Write n PDFs and ground_truth.json into out_dir. Returns the ground truth list."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("CT-*.pdf"):
        f.unlink()
    rng = random.Random(seed)
    anchor = date(2026, 4, 30)
    rows: list[GroundTruth] = []
    for i in range(1, n + 1):
        gt = _make_one(i, rng, anchor)
        _render_pdf(out_dir / f"{gt.contract_id}.pdf", gt)
        rows.append(gt)
    payload = [asdict(r) for r in rows]
    (out_dir / "ground_truth.json").write_text(json.dumps(payload, indent=2))
    return payload


if __name__ == "__main__":
    import sys
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "./generated")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 18
    rows = generate(out, n=n)
    print(f"wrote {len(rows)} contracts to {out}")
