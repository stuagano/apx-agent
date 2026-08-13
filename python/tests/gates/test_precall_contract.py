"""AC-1 gate — the 7-view UC contract is frozen.

Parses the deployable ``customers/mirion/sql/vw_*.sql`` DDLs (the artifacts that
ship to Unity Catalog) and asserts every view name is present and each view's
ordered column list exactly equals the frozen contract in
``customers/mirion/contract.py``. Two representations, one contract — this gate
fails if either drifts.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

# Repo root: python/tests/gates/<this file> -> parents[3].
ROOT = Path(__file__).resolve().parents[3]
MIRION = ROOT / "customers" / "mirion"


def _load_contract():
    spec = importlib.util.spec_from_file_location("mirion_contract", MIRION / "contract.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# CREATE ... VIEW <name> ( <col-list> ) — the parenthesized column contract.
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


@pytest.mark.unit
def test_precall_view_contract_frozen() -> None:
    contract = _load_contract()
    sql_dir = MIRION / "sql"

    parsed: dict[str, list[str]] = {}
    for sql_file in sorted(sql_dir.glob("vw_*.sql")):
        view, cols = _ddl_columns(sql_file.read_text())
        parsed[view] = cols

    # All 7 view names present, exactly the frozen set.
    assert set(parsed) == set(contract.VIEWS), (
        f"DDL view names {sorted(parsed)} != contract {sorted(contract.VIEWS)}"
    )

    # Each view's column set exactly equals the contract — names AND order.
    for view, expected in contract.VIEWS.items():
        assert parsed[view] == expected, (
            f"{view}: DDL columns {parsed[view]} != contract {expected}"
        )
