"""AC-2 gate — synthetic data for all four sources conforms and joins.

Runs the offline generator and asserts, for every one of the 7 views: rows are
non-empty, each row's keys exactly equal the frozen contract columns, and every
``company`` value is drawn from the one shared company seed set — so all 7 brief
sections join cleanly on ``company``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIRION = ROOT / "customers" / "mirion"


def _load(name: str):
    # synthetic.py does `from contract import ...`, so the mirion dir must be importable.
    if str(MIRION) not in sys.path:
        sys.path.insert(0, str(MIRION))
    spec = importlib.util.spec_from_file_location(name, MIRION / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.unit
def test_precall_synthetic_conforms_and_keyed() -> None:
    contract = _load("contract")
    synthetic = _load("synthetic")

    data = synthetic.generate()
    company_set = set(contract.COMPANIES)

    # Same 7 views as the contract.
    assert set(data) == set(contract.VIEWS)

    for view, cols in contract.VIEWS.items():
        rows = data[view]
        assert rows, f"{view}: no synthetic rows (empty view breaks joins)"
        for row in rows:
            # Row conforms exactly to the contract columns — no extra, none missing.
            assert list(row.keys()) == cols, f"{view}: row keys {list(row)} != {cols}"
            # Every company drawn from the one shared seed set.
            assert row["company"] in company_set, (
                f"{view}: company {row['company']!r} not in shared seed set"
            )
        # Every seed company appears in every view -> all sections join cleanly.
        assert {r["company"] for r in rows} == company_set, (
            f"{view}: companies present {sorted({r['company'] for r in rows})} "
            f"!= seed set {sorted(company_set)}"
        )
