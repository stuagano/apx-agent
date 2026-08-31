from __future__ import annotations

from unittest.mock import patch

import pytest
from mlflow.entities import AssessmentSource, AssessmentSourceType
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import NOT_FOUND

from apx_agent import _trace_feedback_api


@pytest.mark.unit
def test_obo_api_binds_host_and_token_per_instance() -> None:
    captured = {}

    class FakeStore:
        def __init__(self, get_host_creds):
            captured["creds"] = get_host_creds()

    with patch(
        "mlflow.store.tracking.databricks_rest_store.DatabricksTracingRestStore",
        FakeStore,
    ):
        _trace_feedback_api._OBOTraceFeedbackApi(
            host="https://workspace.example",
            token="user-token",
        )

    assert captured["creds"].host == "https://workspace.example"
    assert captured["creds"].token == "user-token"


@pytest.mark.unit
def test_obo_api_logs_feedback_through_databricks_tracing_store() -> None:
    calls = []

    class FakeStore:
        def __init__(self, get_host_creds):
            self.creds = get_host_creds()

        def create_assessment(self, assessment):
            calls.append(assessment)
            assessment.assessment_id = "a-1"
            return assessment

    with patch(
        "mlflow.store.tracking.databricks_rest_store.DatabricksTracingRestStore",
        FakeStore,
    ):
        api = _trace_feedback_api._OBOTraceFeedbackApi(
            host="https://workspace.example",
            token="user-token",
        )
        result = api.log_feedback(
            trace_id="tr-1",
            name="quality",
            value=4,
            rationale="Grounded",
            source=AssessmentSource(
                source_type=AssessmentSourceType.HUMAN,
                source_id="reviewer@example.com",
            ),
            metadata={"feature": "claims"},
        )

    assert result.assessment_id == "a-1"
    assert calls[0].trace_id == "tr-1"
    assert calls[0].name == "quality"
    assert calls[0].value == 4
    assert calls[0].rationale == "Grounded"
    assert calls[0].metadata == {"feature": "claims"}


@pytest.mark.unit
def test_obo_api_converts_store_not_found_to_none() -> None:
    class FakeStore:
        def __init__(self, get_host_creds):
            pass

        def get_trace(self, trace_id):
            raise MlflowException("missing", error_code=NOT_FOUND)

    with patch(
        "mlflow.store.tracking.databricks_rest_store.DatabricksTracingRestStore",
        FakeStore,
    ):
        api = _trace_feedback_api._OBOTraceFeedbackApi(
            host="https://workspace.example",
            token="user-token",
        )

    assert api.get_trace("tr-missing") is None
