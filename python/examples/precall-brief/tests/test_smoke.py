"""Offline smoke gate tests for pre-call brief.

AC-1: Frozen view contract (vw_*.sql DDLs match contract.py VIEWS)
AC-2: Synthetic data conforms and joins cleanly on company
AC-3: Brief renders all 7 sections deterministically with seeded values
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

# Repo layout: python/examples/precall-brief/tests/test_smoke.py
# -> parents[1] = precall-brief/
ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    """Load a Python module from the precall-brief root."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.unit
def test_ac1_view_contract_frozen() -> None:
    """Frozen view contract: vw_*.sql DDLs match contract.py VIEWS."""
    contract = _load("contract")
    sql_dir = ROOT / "sql"

    # CREATE ... VIEW <name> ( <col-list> )
    _VIEW_RE = re.compile(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+[\w.]*?(\bvw_\w+)\s*\(([^)]*)\)",
        re.IGNORECASE,
    )

    def _ddl_columns(sql: str) -> tuple[str, list[str]]:
        m = _VIEW_RE.search(sql)
        assert m, f"no CREATE VIEW ... (columns) found in DDL:\n{sql}"
        view = m.group(1)
        cols = [c.strip() for c in m.group(2).split(",") if c.strip()]
        return view, cols

    parsed: dict[str, list[str]] = {}
    for sql_file in sorted(sql_dir.glob("vw_*.sql")):
        view, cols = _ddl_columns(sql_file.read_text())
        parsed[view] = cols

    # All 7 view names present
    assert set(parsed) == set(contract.VIEWS), (
        f"DDL view names {sorted(parsed)} != contract {sorted(contract.VIEWS)}"
    )

    # Each view's columns exactly equal the contract
    for view, expected in contract.VIEWS.items():
        assert parsed[view] == expected, (
            f"{view}: DDL columns {parsed[view]} != contract {expected}"
        )


@pytest.mark.unit
def test_ac2_synthetic_conforms_and_joins() -> None:
    """Synthetic data conforms and joins cleanly on company."""
    contract = _load("contract")
    synthetic = _load("synthetic")

    data = synthetic.generate()
    company_set = set(contract.COMPANIES)

    # Same 7 views as the contract
    assert set(data) == set(contract.VIEWS)

    for view, cols in contract.VIEWS.items():
        rows = data[view]
        assert rows, f"{view}: no synthetic rows (empty view breaks joins)"

        for row in rows:
            # Row conforms exactly to the contract columns
            assert list(row.keys()) == cols, (
                f"{view}: row keys {list(row)} != {cols}"
            )
            # Every company drawn from the shared seed set
            assert row["company"] in company_set, (
                f"{view}: company {row['company']!r} not in shared seed set"
            )

        # Every seed company appears in every view
        assert {r["company"] for r in rows} == company_set, (
            f"{view}: companies present {sorted({r['company'] for r in rows})} "
            f"!= seed set {sorted(company_set)}"
        )


@pytest.mark.unit
def test_ac3_brief_renders_all_sections() -> None:
    """Brief renders all 7 sections deterministically with seeded values."""
    contract = _load("contract")
    synthetic = _load("synthetic")
    brief = _load("brief")

    # Create synthetic data
    data = synthetic.generate(seed=0, rows_per_company=2)

    # Inject as run_view
    def run_view(view: str, company: str) -> list[dict]:
        rows = data[view]
        return [r for r in rows if r["company"] == company]

    # Test with first company
    company = contract.COMPANIES[0]
    markdown = brief.build_brief(company, run_view=run_view, config_path=ROOT / "precall.toml")

    # Assert all 7 section titles present in order
    for title, _ in contract.SECTIONS:
        assert f"## {title}" in markdown, f"Section '{title}' not in brief"

    # Assert company name in header
    assert f"# Pre-Call Brief: {company}" in markdown

    # Assert at least one value from synthetic data (company name in a data row)
    assert company in markdown, f"Company {company!r} not found in brief data"

    # Assert brief has reasonable length (markdown with tables for all 7 sections)
    assert len(markdown) > 500, "Brief too short — may be empty or corrupted"

    # Assert no section is completely empty when data is seeded
    for title, view in contract.SECTIONS:
        rows = run_view(view, company)
        if rows:
            # This section has data, ensure it rendered as a table
            assert f"| " in markdown, f"Section {title} has data but no markdown table"
        else:
            # Section empty, ensure we see "No records."
            assert "No records." in markdown, f"Empty section {title} should say 'No records.'"
