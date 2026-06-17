"""Phase-1 transparency proof: an OKF bundle round-trips payroll-coworker's
real .apx/schema.json with byte-identical grounding outputs."""
from __future__ import annotations

import json
from pathlib import Path

from apx_agent._okf import write_okf_bundle, okf_manifest
from apx_agent._schema import build_instructions_from_schema

REAL_MANIFEST = (
    Path(__file__).resolve().parents[1] / "payroll-coworker" / ".apx" / "schema.json"
)


def _load_real():
    return json.loads(REAL_MANIFEST.read_text())


def test_real_manifest_exists():
    assert REAL_MANIFEST.is_file(), f"expected payroll manifest at {REAL_MANIFEST}"


def test_dict_equality_order_insensitive(tmp_path):
    m = _load_real()
    write_okf_bundle(m, tmp_path / "okf", timestamp="2026-06-16T00:00:00+00:00")
    out = okf_manifest(tmp_path / "okf")
    assert out is not None
    assert out["catalog"] == m["catalog"]
    assert out["schema"] == m["schema"]
    assert out["tables"] == m["tables"]  # all 5 tables, exact col(type) strings


def test_prompt_string_identity(tmp_path):
    m = _load_real()
    write_okf_bundle(m, tmp_path / "okf", timestamp="2026-06-16T00:00:00+00:00")
    okf_tables = okf_manifest(tmp_path / "okf")["tables"]
    before = build_instructions_from_schema(m["catalog"], m["schema"], m["tables"])
    after = build_instructions_from_schema(m["catalog"], m["schema"], okf_tables)
    assert after == before  # byte-identical (order-sensitive _format_schema_block)


def test_committed_bundle_matches_committed_cache():
    # The shipped payroll-coworker OKF bundle must round-trip to its committed
    # schema.json derived cache — guards against bundle/cache drift.
    from apx_agent._okf import okf_manifest

    okf_root = REAL_MANIFEST.parent / "okf"
    assert okf_root.is_dir(), f"payroll OKF bundle missing at {okf_root}"
    assert okf_manifest(okf_root) == _load_real()
