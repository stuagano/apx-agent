"""Claim-vs-reality (ctk) for the handoff tier.

* AC-7 (cheap, always runs): ``lint_bundle`` over the violating fixture yields
  REAL, non-empty violation strings that each name an offending path/identifier
  — asserted by writing the report to disk and reading it back through
  ``ctk.verify(Artifact(...))``, not a bare count or exit-0. The clean fixture
  yields ``[]``.
* AC-8..11 (live, profile-gated): drive ``checks/prove_handoff_ready.py`` against
  a real workspace and read the fact back. Skip unless APX_CAPS_PROFILE +
  APX_CAPS_HANDOFF_BUNDLE are set (AC-9 also needs APX_CAPS_CANARY_ALLOW_MUTATE).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".ctk"))
import ctk  # noqa: E402

from apx_agent import _handoff_lint as hl  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "handoff"
CHECKS = Path(__file__).resolve().parents[1] / "checks" / "prove_handoff_ready.py"


def test_lint_report_is_real_not_exit0(tmp_path: Path) -> None:
    """AC-7: the violating fixture produces real, path-naming violations."""
    violations = hl.lint_bundle(hl.load_bundle(FIXTURES / "violating.yml"))
    report = tmp_path / "handoff_report.txt"
    report.write_text("\n".join(violations) + "\n")

    # Read the report back and prove it carries real evidence — a named
    # offending identifier per category, not merely a non-zero count.
    ctk.verify(
        ctk.Artifact(
            str(report),
            min_bytes=len("timeout_seconds"),
            must_contain="timeout_seconds",
        ),
        ctk.Artifact(str(report), must_contain="author@databricks.com"),
        ctk.Artifact(str(report), must_contain="auto_stop_mins"),
        ctk.Artifact(str(report), must_contain="scale_to_zero"),
        ctk.Artifact(str(report), must_contain="non-fully-qualified"),
        ctk.Artifact(str(report), must_contain="not PAUSED"),
    )
    # Every line names something concrete (a colon/quote/path), never a bare count.
    assert violations and all(("'" in v or ":" in v) for v in violations), violations
    assert hl.lint_bundle(hl.load_bundle(FIXTURES / "clean.yml")) == []


def _live_env() -> tuple[str, str] | None:
    profile = os.environ.get("APX_CAPS_PROFILE")
    bundle = os.environ.get("APX_CAPS_HANDOFF_BUNDLE")
    if profile and bundle:
        return profile, bundle
    return None


def _run_prove(sub: str, artifact: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "APX_CAPS_HANDOFF_ARTIFACT": str(artifact)}
    return subprocess.run(
        [sys.executable, str(CHECKS), sub],
        capture_output=True, text=True, env=env,
    )


def test_live_owner_reads_back_as_sp(tmp_path: Path) -> None:
    """AC-8: each job's run_as reads back as a REAL service principal."""
    if _live_env() is None:
        pytest.skip("live handoff gate: set APX_CAPS_PROFILE + APX_CAPS_HANDOFF_BUNDLE")
    artifact = tmp_path / "owner.json"
    proc = _run_prove("owner-is-sp", artifact)
    assert proc.returncode == 0, f"owner-is-sp disproven/error: {proc.stderr}"
    # Read the SP owners back — a real fact (application_id/name), not exit-0.
    ctk.verify(ctk.Artifact(str(artifact), is_json=True, json_keys=["owner_is_sp"]))
    owners = json.loads(artifact.read_text())["owner_is_sp"]
    assert owners and all(sp for sp in owners.values()), owners


def test_live_clean_deploy_and_readback(tmp_path: Path) -> None:
    """AC-9: opt-in clean deploy stands the set up and reads back."""
    if _live_env() is None:
        pytest.skip("live handoff gate: set APX_CAPS_PROFILE + APX_CAPS_HANDOFF_BUNDLE")
    artifact = tmp_path / "deploy.json"
    proc = _run_prove("clean-deploy", artifact)
    if os.environ.get("APX_CAPS_CANARY_ALLOW_MUTATE") != "1":
        assert proc.returncode == 3, f"expected cannot_run without mutate flag, got {proc.returncode}"
        pytest.skip("clean-deploy needs APX_CAPS_CANARY_ALLOW_MUTATE=1 (mutating deploy)")
    assert proc.returncode == 0, f"clean-deploy disproven/error: {proc.stderr}"
    ctk.verify(ctk.Artifact(str(artifact), is_json=True, json_keys=["matched"]))


def test_live_idle_reads_back(tmp_path: Path) -> None:
    """AC-10: warehouse auto_stop / endpoint scale_to_zero read back enabled."""
    if _live_env() is None:
        pytest.skip("live handoff gate: set APX_CAPS_PROFILE + APX_CAPS_HANDOFF_BUNDLE")
    artifact = tmp_path / "idle.json"
    proc = _run_prove("idle-readback", artifact)
    assert proc.returncode == 0, f"idle-readback disproven/error: {proc.stderr}"
    ctk.verify(ctk.Artifact(str(artifact), is_json=True, json_keys=["idle"]))


def test_live_inventory_lists_back(tmp_path: Path) -> None:
    """AC-11: the live inventory is non-empty and matches the declared set."""
    if _live_env() is None:
        pytest.skip("live handoff gate: set APX_CAPS_PROFILE + APX_CAPS_HANDOFF_BUNDLE")
    artifact = tmp_path / "inventory.json"
    proc = _run_prove("inventory", artifact)
    assert proc.returncode == 0, f"inventory disproven/error: {proc.stderr}"
    ctk.verify(ctk.Artifact(str(artifact), is_json=True, json_keys=["declared", "matched"]))
    data = json.loads(artifact.read_text())
    assert data["matched"], data
