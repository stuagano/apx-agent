"""Phase-2 proof on the real payroll-coworker bundle: enrichment reaches the
prompt additively; an un-enriched bundle stays byte-identical to Phase 1."""
from __future__ import annotations

import json
from pathlib import Path

from apx_agent._okf import write_okf_bundle, okf_manifest, okf_grounding
from apx_agent._schema import build_instructions_from_schema

REAL_MANIFEST = Path(__file__).resolve().parents[1] / "payroll-coworker" / ".apx" / "schema.json"


def test_unenriched_bundle_prompt_identical(tmp_path):
    m = json.loads(REAL_MANIFEST.read_text())
    write_okf_bundle(m, tmp_path / "okf", timestamp="z")  # bare auto-gen, no enrichment
    tables = okf_manifest(tmp_path / "okf")["tables"]
    grounding = okf_grounding(tmp_path / "okf")
    assert grounding is None
    plain = build_instructions_from_schema(m["catalog"], m["schema"], tables)
    grounded = build_instructions_from_schema(m["catalog"], m["schema"], tables, grounding=grounding)
    assert grounded == plain


def test_committed_enriched_bundle_surfaces_in_prompt():
    okf_root = REAL_MANIFEST.parent / "okf"
    m = okf_manifest(okf_root)
    grounding = okf_grounding(okf_root)
    assert grounding is not None and "pay_runs" in grounding
    out = build_instructions_from_schema(m["catalog"], m["schema"], m["tables"], grounding=grounding)
    assert "# Joins" not in out  # distilled prose, not raw headings
    assert "employee_id" in out
    assert "- employees:" in out  # an un-enriched table line is still present and plain
