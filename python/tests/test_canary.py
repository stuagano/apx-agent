"""Tests for _canary.py — multi-version traffic split + analysis.

Covers:
  - get_canary_config reads served_entities + traffic_config off the SDK
    response shape
  - deploy_canary builds the right ServedEntityInput list + TrafficConfig
    with the canary at the requested percentage and existing entities
    proportionally redistributed
  - deploy_canary edge cases: brand-new endpoint, single existing
    served entity, rounding drift reconciliation
  - promote_canary / rollback_canary send 100% to the target, keep
    other entities at 0%
  - promote_canary refuses to promote an entity that isn't on the
    endpoint
  - analyze_canary partitions traces by version, computes
    requests / errors / latency P50 / P95 / avg per version
  - CanaryReport.best_by_latency / best_by_error_rate tie-breaking
  - Trace partition handles missing version attributes (falls back to
    "unknown") and tolerates older Trace-object + DataFrame shapes
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("mlflow")

from apx_agent import (
    CanaryConfig,
    CanaryReport,
    VersionMetrics,
    analyze_canary,
    deploy_canary,
    get_canary_config,
    promote_canary,
    rollback_canary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _se(name: str, entity_name: str, entity_version: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        entity_name=entity_name,
        entity_version=entity_version,
    )


def _route(name: str, pct: int) -> SimpleNamespace:
    return SimpleNamespace(served_entity_name=name, traffic_percentage=pct)


def _endpoint_response(
    *,
    served_entities: list[SimpleNamespace],
    routes: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            served_entities=served_entities,
            traffic_config=SimpleNamespace(routes=routes or []),
        ),
    )


# ---------------------------------------------------------------------------
# get_canary_config
# ---------------------------------------------------------------------------


def test_get_canary_config_reads_entities_and_split() -> None:
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_response(
        served_entities=[
            _se("triage-1", "main.agents.triage", "1"),
            _se("triage-2", "main.agents.triage", "2"),
        ],
        routes=[_route("triage-1", 90), _route("triage-2", 10)],
    )

    cfg = get_canary_config("triage", ws=ws)

    assert cfg.endpoint == "triage"
    assert cfg.served_entities == (
        ("triage-1", "main.agents.triage", "1"),
        ("triage-2", "main.agents.triage", "2"),
    )
    assert cfg.traffic_split == {"triage-1": 90, "triage-2": 10}


def test_get_canary_config_handles_empty_endpoint() -> None:
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = SimpleNamespace(
        config=None, pending_config=None,
    )

    cfg = get_canary_config("triage", ws=ws)

    assert cfg.served_entities == ()
    assert cfg.traffic_split == {}


# ---------------------------------------------------------------------------
# deploy_canary
# ---------------------------------------------------------------------------


def test_deploy_canary_rejects_invalid_traffic_pct() -> None:
    with pytest.raises(ValueError):
        deploy_canary(
            endpoint="x", registered_model_name="main.x.y",
            new_version=2, canary_traffic_pct=0, ws=MagicMock(),
        )
    with pytest.raises(ValueError):
        deploy_canary(
            endpoint="x", registered_model_name="main.x.y",
            new_version=2, canary_traffic_pct=100, ws=MagicMock(),
        )


def test_deploy_canary_redistributes_existing_split_proportionally() -> None:
    ws = MagicMock()
    # Current: triage-1=100% (only entity)
    ws.serving_endpoints.get.return_value = _endpoint_response(
        served_entities=[_se("triage-1", "main.agents.triage", "1")],
        routes=[_route("triage-1", 100)],
    )

    deploy_canary(
        endpoint="triage",
        registered_model_name="main.agents.triage",
        new_version=2,
        canary_traffic_pct=10,
        ws=ws,
    )

    # update_config call should include both served entities + a split where
    # canary gets 10%, existing gets 90%.
    update_kwargs = ws.serving_endpoints.update_config.call_args.kwargs
    assert update_kwargs["name"] == "triage"
    entities = update_kwargs["served_entities"]
    names = {e.name for e in entities}
    assert names == {"triage-1", "triage-2"}
    routes = update_kwargs["traffic_config"].routes
    split = {r.served_entity_name: r.traffic_percentage for r in routes}
    assert split == {"triage-1": 90, "triage-2": 10}


def test_deploy_canary_proportional_split_across_existing() -> None:
    """With two existing entities at 70/30, adding a 10% canary should yield 63/27/10."""
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_response(
        served_entities=[
            _se("triage-1", "main.agents.triage", "1"),
            _se("triage-2", "main.agents.triage", "2"),
        ],
        routes=[_route("triage-1", 70), _route("triage-2", 30)],
    )

    deploy_canary(
        endpoint="triage",
        registered_model_name="main.agents.triage",
        new_version=3,
        canary_traffic_pct=10,
        ws=ws,
    )

    routes = ws.serving_endpoints.update_config.call_args.kwargs["traffic_config"].routes
    split = {r.served_entity_name: r.traffic_percentage for r in routes}
    assert split["triage-3"] == 10
    # 70% of 90 = 63, 30% of 90 = 27 — sum to 90, plus 10 canary = 100
    assert sum(split.values()) == 100
    # 63/27 (allowing rounding drift up to 1)
    assert abs(split["triage-1"] - 63) <= 1
    assert abs(split["triage-2"] - 27) <= 1


def test_deploy_canary_to_brand_new_endpoint_just_creates_the_canary() -> None:
    """When no served entities exist yet, the canary takes its declared %; remaining is unallocated."""
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_response(served_entities=[], routes=[])

    deploy_canary(
        endpoint="newone",
        registered_model_name="main.agents.newone",
        new_version=1,
        canary_traffic_pct=50,
        ws=ws,
    )

    routes = ws.serving_endpoints.update_config.call_args.kwargs["traffic_config"].routes
    # Only the new entity in the routes; existing-entities-loop ran zero times
    names = {r.served_entity_name for r in routes}
    assert names == {"newone-1"}


def test_deploy_canary_does_not_duplicate_existing_target() -> None:
    """If the target version is already a served entity, dedupe — don't create a dup."""
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_response(
        served_entities=[
            _se("triage-1", "main.agents.triage", "1"),
            _se("triage-2", "main.agents.triage", "2"),
        ],
        routes=[_route("triage-1", 100)],
    )

    deploy_canary(
        endpoint="triage",
        registered_model_name="main.agents.triage",
        new_version=2,  # same as existing triage-2
        canary_traffic_pct=20,
        ws=ws,
    )

    entities = ws.serving_endpoints.update_config.call_args.kwargs["served_entities"]
    names = [e.name for e in entities]
    # triage-2 appears once
    assert names.count("triage-2") == 1


# ---------------------------------------------------------------------------
# promote_canary / rollback_canary
# ---------------------------------------------------------------------------


def test_promote_canary_sets_100_pct_to_target() -> None:
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_response(
        served_entities=[
            _se("triage-1", "main.agents.triage", "1"),
            _se("triage-2", "main.agents.triage", "2"),
        ],
        routes=[_route("triage-1", 90), _route("triage-2", 10)],
    )

    promote_canary(
        endpoint="triage",
        registered_model_name="main.agents.triage",
        version=2,
        ws=ws,
    )

    routes = ws.serving_endpoints.update_config.call_args.kwargs["traffic_config"].routes
    split = {r.served_entity_name: r.traffic_percentage for r in routes}
    assert split == {"triage-1": 0, "triage-2": 100}


def test_promote_canary_returns_requested_split_not_stale_read() -> None:
    """Claim-vs-reality: update_config is async, so a re-read returns the still-active
    (pre-update) split. The returned CanaryConfig — which the CLI echoes as the new
    split — must reflect what was REQUESTED, not the stale read.
    """
    ws = MagicMock()
    # The mock `get` returns the OLD 90/10 split on every call, including the
    # post-mutation re-read (the real async-update staleness).
    ws.serving_endpoints.get.return_value = _endpoint_response(
        served_entities=[
            _se("triage-1", "main.agents.triage", "1"),
            _se("triage-2", "main.agents.triage", "2"),
        ],
        routes=[_route("triage-1", 90), _route("triage-2", 10)],
    )

    cfg = promote_canary(
        endpoint="triage",
        registered_model_name="main.agents.triage",
        version=2,
        ws=ws,
    )

    # Must be the requested split (100% to v2), NOT the stale 90/10 re-read.
    assert cfg.traffic_split == {"triage-1": 0, "triage-2": 100}


def test_promote_canary_refuses_unknown_target() -> None:
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_response(
        served_entities=[_se("triage-1", "main.agents.triage", "1")],
        routes=[_route("triage-1", 100)],
    )

    with pytest.raises(ValueError, match="isn't a served entity"):
        promote_canary(
            endpoint="triage",
            registered_model_name="main.agents.triage",
            version=99,
            ws=ws,
        )


def test_rollback_canary_is_promote() -> None:
    """rollback_canary is functionally identical to promote_canary."""
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = _endpoint_response(
        served_entities=[
            _se("triage-1", "main.agents.triage", "1"),
            _se("triage-2", "main.agents.triage", "2"),
        ],
        routes=[_route("triage-1", 90), _route("triage-2", 10)],
    )

    rollback_canary(
        endpoint="triage",
        registered_model_name="main.agents.triage",
        version=1,  # roll back to prior version
        ws=ws,
    )

    routes = ws.serving_endpoints.update_config.call_args.kwargs["traffic_config"].routes
    split = {r.served_entity_name: r.traffic_percentage for r in routes}
    assert split == {"triage-1": 100, "triage-2": 0}


# ---------------------------------------------------------------------------
# analyze_canary
# ---------------------------------------------------------------------------


def _trace(
    *,
    version: str,
    status: str = "OK",
    latency_ms: int = 100,
    endpoint: str = "triage",
) -> dict[str, object]:
    return {
        "trace_id": f"tr-{version}-{latency_ms}",
        "tags": {
            "apx.model.endpoint": endpoint,
            "apx.model.endpoint_version": version,
        },
        "status": status,
        "execution_time_ms": latency_ms,
    }


def _df(rows: list[dict[str, object]]) -> SimpleNamespace:
    return SimpleNamespace(to_dict=lambda orient: rows)


def test_analyze_canary_partitions_by_version() -> None:
    rows = [
        _trace(version="1", latency_ms=100),
        _trace(version="1", latency_ms=150),
        _trace(version="2", latency_ms=80),
        _trace(version="2", latency_ms=90),
        _trace(version="2", latency_ms=110),
    ]

    with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", return_value=_df(rows)):
        report = analyze_canary(
            endpoint="triage",
            experiment="/x",
            ws=MagicMock(),
        )

    versions = {v.version: v for v in report.versions}
    assert versions["1"].requests == 2
    assert versions["2"].requests == 3
    assert versions["1"].errors == 0
    assert versions["2"].errors == 0


def test_analyze_canary_counts_errors() -> None:
    rows = [
        _trace(version="1", status="OK"),
        _trace(version="1", status="ERROR"),
        _trace(version="1", status="FAILED"),
    ]

    with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", return_value=_df(rows)):
        report = analyze_canary(
            endpoint="triage", experiment="/x", ws=MagicMock(),
        )

    v1 = next(v for v in report.versions if v.version == "1")
    assert v1.errors == 2
    assert v1.requests == 3
    assert v1.error_rate == pytest.approx(2 / 3)


def test_analyze_canary_computes_latency_percentiles() -> None:
    # 100 traces at 100ms each — easier to assert percentile shape.
    rows = [_trace(version="1", latency_ms=100 + i) for i in range(100)]

    with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", return_value=_df(rows)):
        report = analyze_canary(
            endpoint="triage", experiment="/x", ws=MagicMock(),
        )

    v = report.versions[0]
    assert 145 <= (v.latency_p50_ms or 0) <= 155
    assert 190 <= (v.latency_p95_ms or 0) <= 200


def test_analyze_canary_filters_to_endpoint() -> None:
    rows = [
        _trace(version="1", endpoint="triage"),
        _trace(version="1", endpoint="other-endpoint"),  # different endpoint, dropped
    ]

    with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", return_value=_df(rows)):
        report = analyze_canary(
            endpoint="triage", experiment="/x", ws=MagicMock(),
        )

    assert sum(v.requests for v in report.versions) == 1


def test_analyze_canary_unknown_version_when_attribute_missing() -> None:
    rows = [{"trace_id": "tr-1", "tags": {}, "status": "OK", "execution_time_ms": 50}]

    with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", return_value=_df(rows)):
        report = analyze_canary(
            endpoint="triage", experiment="/x", ws=MagicMock(),
        )

    assert any(v.version == "unknown" for v in report.versions)


def test_analyze_canary_returns_empty_report_when_search_traces_fails() -> None:
    # A blocked-blob / bad-token read must NOT collapse into a clean empty
    # report (which reads as "no errors observed" and could greenlight a bad
    # model). The failure is surfaced as a RuntimeError that names the
    # experiment and chains the original cause.
    with patch("apx_agent._mlflow_tracing.search_traces_for_experiment", side_effect=RuntimeError("backend down")):
        with pytest.raises(RuntimeError, match="search_traces failed") as exc_info:
            analyze_canary(
                endpoint="triage", experiment="/x", ws=MagicMock(),
            )

    assert "/x" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "backend down" in str(exc_info.value.__cause__)


# ---------------------------------------------------------------------------
# CanaryReport best-of helpers
# ---------------------------------------------------------------------------


def test_best_by_latency_picks_lowest_p95() -> None:
    report = CanaryReport(
        endpoint="triage", lookback_hours=24,
        versions=(
            VersionMetrics(version="1", requests=10, errors=0,
                           latency_p50_ms=80, latency_p95_ms=200, latency_avg_ms=100),
            VersionMetrics(version="2", requests=10, errors=0,
                           latency_p50_ms=90, latency_p95_ms=150, latency_avg_ms=110),
        ),
    )
    best = report.best_by_latency()
    assert best is not None
    assert best.version == "2"


def test_best_by_error_rate_picks_lowest_rate() -> None:
    report = CanaryReport(
        endpoint="triage", lookback_hours=24,
        versions=(
            VersionMetrics(version="1", requests=100, errors=5,
                           latency_p50_ms=100, latency_p95_ms=200, latency_avg_ms=120),
            VersionMetrics(version="2", requests=100, errors=2,
                           latency_p50_ms=100, latency_p95_ms=200, latency_avg_ms=120),
        ),
    )
    best = report.best_by_error_rate()
    assert best is not None
    assert best.version == "2"


def test_canary_report_handles_no_versions() -> None:
    report = CanaryReport(endpoint="x", lookback_hours=1, versions=())
    assert report.best_by_latency() is None
    assert report.best_by_error_rate() is None
