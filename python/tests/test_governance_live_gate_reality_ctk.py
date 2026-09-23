"""Opt-in LIVE proof (#808): governance -> DQX loop, runtime decision surface.

Proves the final link of the loop end-to-end with live data: a governed
apx-agent tool call (real ``GovernanceGuard.for_tool`` hook) is decided by
the companion governance repo's real runtime surface — ``fetch_posture``
reading the live ``resource_inventory`` posture written by the crawler from
a real DQX run, ``evaluate_operation`` applying the real YAML policies
(POL-Q007/Q011, guardrails-declared) — and the decision lands on the trace
span as ``apx.governance.*`` attributes, including the real quality score.

This test never runs in default CI. It skips — with an explicit UNVERIFIED
reason — unless the operator provides:

  APX_LIVE_GOVERNANCE_PROFILE   Databricks CLI profile with access to the
                                sandbox schema (serverless Spark Connect)
  APX_LIVE_GOVERNANCE_CATALOG   (optional) default ``home_stuart_gano``
  APX_LIVE_GOVERNANCE_SCHEMA    (optional) default ``apx_dqx_enforce``
  APX_LIVE_GOVERNANCE_TABLE     (optional) default ``user_reviews``
  APX_GOVERNANCE_REPO           (optional) path to the companion
                                governance-monitoring checkout; defaults to
                                ``../databricks-watchdog`` next to this repo

Prerequisite (operator-owned): the sandbox posture must have been produced
by the real pipeline (compile POL-Q010 -> DQX run -> crawler enrichment),
leaving ``dqx_quality_score`` / ``dqx_anomalies`` / ``data_layer=gold`` on
the inventory row. The driver script ``.ctk/run_live_gate.py`` performs the
same proof interactively.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _config() -> dict[str, str]:
    profile = _env("APX_LIVE_GOVERNANCE_PROFILE")
    if profile is None:
        pytest.skip(
            "APX-LIVE-GATE UNVERIFIED (not configured): set "
            "APX_LIVE_GOVERNANCE_PROFILE to a Databricks CLI profile with "
            "access to the governance sandbox schema."
        )
    assert profile is not None
    default_repo = Path(__file__).parent.parent.parent.parent / "databricks-watchdog"
    return {
        "profile": profile,
        "catalog": _env("APX_LIVE_GOVERNANCE_CATALOG") or "home_stuart_gano",
        "schema": _env("APX_LIVE_GOVERNANCE_SCHEMA") or "apx_dqx_enforce",
        "table": _env("APX_LIVE_GOVERNANCE_TABLE") or "user_reviews",
        "repo": _env("APX_GOVERNANCE_REPO") or str(default_repo),
    }


class _SpanRecorder:
    """Stand-in for the active MLflow span — records set_attribute calls.

    The decision path under test is entirely real; only the tracer is
    replaced, since no MLflow run is active in a test process.
    """

    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value


def test_live_governance_gate_renders_real_quality_score() -> None:
    cfg = _config()
    engine_src = str(Path(cfg["repo"]) / "engine" / "src")
    policies_dir = str(Path(cfg["repo"]) / "engine" / "policies")
    if not Path(engine_src).is_dir() or not Path(policies_dir).is_dir():
        pytest.skip(
            "APX-LIVE-GATE UNVERIFIED: companion governance checkout not found "
            f"at {cfg['repo']} (set APX_GOVERNANCE_REPO)."
        )

    databricks_connect = pytest.importorskip(
        "databricks.connect", reason="live proof needs serverless Spark Connect"
    )

    sys.path.insert(0, engine_src)
    try:
        from governance.ontology import OntologyEngine
        from governance.policy_loader import load_yaml_policies
        from governance.rule_engine import RuleEngine
        from governance.runtime import evaluate_operation, fetch_posture
    except ImportError as e:
        pytest.skip(f"APX-LIVE-GATE UNVERIFIED: companion runtime unavailable: {e}")
        return

    from apx_agent import GovernanceClient, GovernanceGuard
    import apx_agent._governance as gov

    table = f"{cfg['catalog']}.{cfg['schema']}.{cfg['table']}"
    spark = (
        databricks_connect.DatabricksSession.builder
        .profile(cfg["profile"])
        .serverless(True)
        .getOrCreate()
    )
    try:
        policies = load_yaml_policies(policies_dir)
        if not policies:
            pytest.skip(
                "APX-LIVE-GATE UNVERIFIED: no policies loaded from companion "
                f"checkout ({policies_dir})."
            )
            return
        ontology = OntologyEngine()
        rule_engine = RuleEngine()

        def live_transport(request: dict[str, Any]) -> dict[str, Any]:
            if request.get("type") == "violation_report":
                return {}
            posture = fetch_posture(
                spark, cfg["catalog"], cfg["schema"], [table]
            )
            if table not in posture:
                pytest.skip(
                    "APX-LIVE-GATE UNVERIFIED: no inventory posture for "
                    f"{table} — run the compile/DQX/crawl pipeline first."
                )
            return evaluate_operation(
                request["operation"], [table], posture,
                policies, ontology, rule_engine,
            )

        guard = GovernanceGuard(
            GovernanceClient(transport=live_transport),
            agent_name="apx_live_gate_proof",
        )
        check = guard.for_tool()

        span = _SpanRecorder()
        orig = gov.current_active_span
        gov.current_active_span = lambda: span
        try:
            check("query_reviews", {"table": table})
        except PermissionError:
            pass  # reject is the expected live outcome with degraded posture
        finally:
            gov.current_active_span = orig
    finally:
        spark.stop()

    attrs = span.attrs
    # The decision must be governance-driven (one of the guardrails-declared
    # quality policies), never a silent pass-through.
    assert attrs.get("apx.governance.action") in ("allow", "reject")
    score = attrs.get("apx.governance.quality_score")
    assert isinstance(score, (int, float)), (
        f"expected a real quality score on the span, got attrs={attrs}"
    )
    if attrs["apx.governance.action"] == "reject":
        assert attrs.get("apx.governance.policy_id") in ("POL-Q007", "POL-Q011")
        assert attrs.get("apx.governance.domain") == "DataQuality"
