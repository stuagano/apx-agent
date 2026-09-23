"""Unit tests for DQX resource metrics (dq_status) in the Ontos adapter.

Covers the ResourceMetric model, ResourceDetail.metrics default, and
GovernanceProvider.get_resource metric population including the
graceful fallback when dq_status does not exist (crawler without
DQX metrics tables configured).

Run with: pytest adapters/ontos/tests/test_ontos_resource_metrics.py -v
"""

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

# Ensure the adapter package is importable
ADAPTER_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ADAPTER_ROOT / "src"))


def _ensure_databricks_sql_importable():
    """Stub out databricks.sql if not installed (same pattern as
    test_metastore_awareness.py)."""
    if "databricks.sql" not in sys.modules:
        db_sql = ModuleType("databricks.sql")
        db_sql.connect = MagicMock()
        sys.modules["databricks.sql"] = db_sql

        if "databricks" in sys.modules:
            sys.modules["databricks"].sql = db_sql


_ensure_databricks_sql_importable()

import pytest
from ontos_governance.models import ResourceDetail, ResourceMetric
from pydantic import ValidationError

# ── ResourceMetric model ────────────────────────────────────────────────────


class TestResourceMetricModel:
    def test_valid_metric(self):
        m = ResourceMetric(
            source="dqx",
            metric="quality_score",
            value="97.5",
            status="ok",
            anomaly=False,
            checked_at="2026-09-20 14:03:11",
        )
        assert m.source == "dqx"
        assert m.metric == "quality_score"
        assert m.value == "97.5"

    def test_optional_fields_default_none(self):
        m = ResourceMetric(source="dqx", metric="quality_score")
        assert m.value is None
        assert m.status is None
        assert m.anomaly is None
        assert m.checked_at is None

    def test_requires_source_and_metric(self):
        with pytest.raises(ValidationError):
            ResourceMetric(value="97.5")

    def test_anomaly_coerces_from_string(self):
        m = ResourceMetric(source="dqx", metric="quality_score", anomaly="true")
        assert m.anomaly is True


class TestResourceDetailMetrics:
    def test_metrics_defaults_to_empty(self):
        r = ResourceDetail(
            resource_id="gold.finance.gl",
            resource_name="gl",
            resource_type="TABLE",
            first_seen="2026-01-01",
            last_seen="2026-09-20",
            scan_id="scan-1",
            classifications=[],
            violations=[],
            exceptions=[],
        )
        assert r.metrics == []

    def test_metrics_round_trip(self):
        r = ResourceDetail(
            resource_id="gold.finance.gl",
            resource_name="gl",
            resource_type="TABLE",
            first_seen="2026-01-01",
            last_seen="2026-09-20",
            scan_id="scan-1",
            classifications=[],
            violations=[],
            exceptions=[],
            metrics=[
                {"source": "dqx", "metric": "quality_score", "value": "97.5"},
            ],
        )
        assert len(r.metrics) == 1
        assert isinstance(r.metrics[0], ResourceMetric)
        assert r.model_dump()["metrics"][0]["value"] == "97.5"


# ── Provider get_resource ───────────────────────────────────────────────────

_INV_ROW = {
    "resource_id": "gold.finance.gl",
    "resource_name": "gl",
    "resource_type": "TABLE",
    "first_seen": "2026-01-01 00:00:00",
    "last_seen": "2026-09-20 00:00:00",
    "scan_id": "scan-1",
    "metadata": None,
}

_METRIC_ROWS = [
    {
        "source": "dqx",
        "metric": "quality_score",
        "value": "97.5",
        "status": "ok",
        "anomaly": False,
        "checked_at": "2026-09-20 14:03:11",
    },
    {
        "source": "dqx",
        "metric": "row_count_anomaly",
        "value": None,
        "status": "ok",
        "anomaly": False,
        "checked_at": "2026-09-20 14:03:11",
    },
]


def _make_provider():
    from ontos_governance.providers.governance import GovernanceProvider

    return GovernanceProvider(
        server_hostname="host", http_path="/path", access_token="tok"
    )


class TestGetResourceMetrics:
    def test_metrics_populated(self, monkeypatch):
        provider = _make_provider()

        def fake_execute(query):
            if "resource_inventory" in query:
                return [dict(_INV_ROW)]
            if "dq_status" in query:
                return [dict(r) for r in _METRIC_ROWS]
            return []  # classifications, violations, exceptions

        monkeypatch.setattr(provider, "_execute", fake_execute)

        detail = provider.get_resource("gold.finance.gl")

        assert len(detail.metrics) == 2
        by_metric = {m.metric: m for m in detail.metrics}
        assert by_metric["quality_score"].value == "97.5"
        assert by_metric["quality_score"].checked_at == "2026-09-20 14:03:11"
        assert by_metric["row_count_anomaly"].value is None

    def test_missing_dq_status_table_returns_empty_metrics(self, monkeypatch):
        provider = _make_provider()

        def fake_execute(query):
            if "resource_inventory" in query:
                return [dict(_INV_ROW)]
            if "dq_status" in query:
                raise Exception("Table or view not found: dq_status")
            return []

        monkeypatch.setattr(provider, "_execute", fake_execute)

        detail = provider.get_resource("gold.finance.gl")

        assert detail.metrics == []
        assert detail.resource_id == "gold.finance.gl"

    def test_dq_status_query_filters_by_table_id(self, monkeypatch):
        provider = _make_provider()
        seen_queries = []

        def fake_execute(query):
            seen_queries.append(query)
            if "resource_inventory" in query:
                return [dict(_INV_ROW)]
            return []

        monkeypatch.setattr(provider, "_execute", fake_execute)

        provider.get_resource("gold.finance.gl")

        dq_queries = [q for q in seen_queries if "dq_status" in q]
        assert len(dq_queries) == 1
        assert "table_id = 'gold.finance.gl'" in dq_queries[0]
