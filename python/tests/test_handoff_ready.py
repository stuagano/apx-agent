"""Cheap, CI-safe static handoff checks (AC-1..6).

Each test loads a fixture bundle and asserts the matching ``_handoff_lint``
function flags the violating bundle (naming the offending path/identifier) and
returns ``[]`` for the clean one. AC-6 proves the ``handoff`` tier is registered
across manifest + CLI and that a handoff ledger entry is first-class.
"""
from __future__ import annotations

import sys
from pathlib import Path

from apx_agent import _handoff_lint as hl

FIXTURES = Path(__file__).parent / "fixtures" / "handoff"


def _clean() -> dict:
    return hl.load_bundle(FIXTURES / "clean.yml")


def _violating() -> dict:
    return hl.load_bundle(FIXTURES / "violating.yml")


def test_all_jobs_have_timeouts() -> None:
    v = hl.check_job_timeouts(_violating())
    assert any("ingest" in m and "load" in m and "timeout_seconds" in m for m in v), v
    assert hl.check_job_timeouts(_clean()) == []


def test_warehouse_and_endpoint_idle() -> None:
    v = hl.check_warehouse_autostop_and_scale_to_zero(_violating())
    assert any("reporting" in m and "auto_stop_mins" in m for m in v), v
    assert any("agent-llm" in m and "scale_to_zero" in m for m in v), v
    assert hl.check_warehouse_autostop_and_scale_to_zero(_clean()) == []


def test_no_personal_owner() -> None:
    v = hl.check_no_personal_owner(_violating())
    assert any("author@databricks.com" in m for m in v), v
    assert hl.check_no_personal_owner(_clean()) == []


def test_tables_are_fully_qualified() -> None:
    v = hl.check_tables_fqn(_violating())
    assert any("raw_orders" in m for m in v), v
    assert any("orders" in m for m in v), v
    # a fully-qualified reference in the clean bundle is not flagged
    assert hl.check_tables_fqn(_clean()) == []


def test_schedules_paused_or_owned() -> None:
    v = hl.check_schedules_paused_or_owned(_violating())
    assert any("ingest" in m and "PAUSED" in m for m in v), v
    # clean: one schedule is PAUSED, the other is UNPAUSED but SP-owned
    assert hl.check_schedules_paused_or_owned(_clean()) == []


def _load_root_caps(module: str):
    """Load a repo-root caps module by path.

    ``make check`` and ``python -m caps`` resolve the tracked repo-root ``caps/``
    (via ``sys.path.insert(0, '..')``), while pytest's ``pythonpath=['.ctk']``
    shadows ``caps`` with the vendored ctk snapshot. AC-6 must assert the tracked
    files the PRD anchors, so load them directly by path.
    """
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    path = root / "caps" / f"{module}.py"
    name = f"_root_caps_{module}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses.asdict can resolve the class' __module__.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_handoff_tier_registered_and_ledgered() -> None:
    manifest_mod = _load_root_caps("manifest")
    ledger_mod = _load_root_caps("ledger")

    assert "handoff" in manifest_mod.VALID_TIERS
    assert manifest_mod.DEFAULT_FRESHNESS["handoff"] == "code"

    # CLI declares --tier handoff in all three subcommands (status/verify/add).
    root = Path(__file__).resolve().parents[2]
    cli_src = (root / "caps" / "cli.py").read_text()
    assert cli_src.count('choices=["cheap", "live", "handoff"]') == 3, (
        "handoff must be an accepted --tier choice in status/verify/add"
    )

    # A manifest entry with tier: handoff parses (freshness defaults to code).
    manifest = FIXTURES / "_tier_probe.yaml"
    manifest.write_text(
        "capabilities:\n"
        "  - id: handoff-probe\n"
        "    description: probe\n"
        "    given: a bundle\n"
        "    when: linted\n"
        "    then: clean\n"
        "    tier: handoff\n"
        "    check: tests/test_handoff_ready.py::test_all_jobs_have_timeouts\n"
    )
    try:
        caps = manifest_mod.load_manifest(manifest)
        assert caps[0].tier == "handoff"
        assert caps[0].freshness == "code"
        assert caps[0].check_kind == "pytest"
    finally:
        manifest.unlink()

    # A handoff ledger entry is first-class: tier round-trips through save/load.
    led = FIXTURES / "_ledger_probe.json"
    entry = ledger_mod.LedgerEntry(
        result="pass", at="2026-09-11T00:00:00+00:00", tier="handoff"
    )
    try:
        ledger_mod.save_ledger(led, {"handoff-probe": entry})
        assert ledger_mod.load_ledger(led)["handoff-probe"].tier == "handoff"
    finally:
        led.unlink()


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
