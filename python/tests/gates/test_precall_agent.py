"""AC-3 gate — headless brief has all 7 sections and seeded per-company data.

Builds the brief from ``customers/mirion.toml`` for a seeded company, with SQL
stubbed by the offline synthetic generator, and asserts every one of the 7
section titles is present and the seeded per-company data values render into the
markdown (and no other company's rows leak in).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIRION = ROOT / "customers" / "mirion"


def _load(name: str):
    if str(MIRION) not in sys.path:
        sys.path.insert(0, str(MIRION))
    spec = importlib.util.spec_from_file_location(name, MIRION / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.unit
def test_precall_brief_has_all_seven_sections() -> None:
    contract = _load("contract")
    synthetic = _load("synthetic")
    brief = _load("brief")

    data = synthetic.generate()
    company = contract.COMPANIES[0]
    other = contract.COMPANIES[1]

    # Stubbed SQL: filter the synthetic view rows to the requested company.
    def run_view(view: str, who: str) -> list[dict]:
        return [r for r in data[view] if r["company"] == who]

    md = brief.build_brief(company, run_view)

    # All 7 configured section titles present, and it's the right company.
    titles = [title for title, _ in brief.load_sections()]
    assert len(titles) == 7
    for title in titles:
        assert f"## {title}" in md, f"missing section: {title}"
    assert f"# Pre-Call Brief: {company}" in md

    # Seeded per-company data values render into the brief. Check a distinctive
    # non-company value from every view (e.g. order_id, opportunity, rma_id).
    for view, cols in contract.VIEWS.items():
        rows = run_view(view, company)
        assert rows, f"no seeded rows for {company} in {view}"
        value_col = cols[1]  # first non-'company' contract column
        for row in rows:
            assert str(row[value_col]) in md, (
                f"{view}.{value_col}={row[value_col]!r} missing from brief"
            )

    # No other company's rows leaked into this brief.
    assert other not in md
