from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apx_agent import _trace_feedback


@pytest.mark.unit
def test_attach_feedback_logs_human_assessment_with_metadata() -> None:
    calls: list[dict] = []
    api = SimpleNamespace(
        log_feedback=lambda **kwargs: (
            calls.append(kwargs),
            SimpleNamespace(assessment_id="a-1"),
        )[1]
    )

    result = _trace_feedback.attach_feedback(
        _trace_feedback.TraceFeedback(
            trace_id="tr-1",
            name="domain_quality",
            value=4,
            comment="Correct answer, weak rationale",
            source="review-app",
            evidence={"feature": "claims_search"},
        ),
        mlflow_api=api,
    )

    assert result == _trace_feedback.TraceFeedbackResult(
        trace_id="tr-1", feedback_id="a-1", name="domain_quality", created=True
    )
    assert calls[0]["trace_id"] == "tr-1"
    assert calls[0]["name"] == "domain_quality"
    assert calls[0]["value"] == 4
    assert calls[0]["rationale"] == "Correct answer, weak rationale"
    assert calls[0]["metadata"] == {"feature": "claims_search"}
    assert calls[0]["source"].source_type == "HUMAN"
    assert calls[0]["source"].source_id == "review-app"


@pytest.mark.unit
def test_attach_feedback_reuses_matching_idempotency_key() -> None:
    assessment = SimpleNamespace(
        to_dictionary=lambda: {
            "assessment_id": "a-existing",
            "assessment_name": "domain_quality",
            "feedback": {"value": 4},
            "source": {"source_type": "HUMAN", "source_id": "review-app"},
            "metadata": {
                _trace_feedback.IDEMPOTENCY_METADATA_KEY: "external-row-123"
            },
        }
    )
    trace = SimpleNamespace(
        info=SimpleNamespace(trace_id="tr-1", tags={}, assessments=[assessment])
    )

    def unexpected_write(**kwargs):
        raise AssertionError(f"unexpected write: {kwargs}")

    api = SimpleNamespace(get_trace=lambda trace_id: trace, log_feedback=unexpected_write)
    result = _trace_feedback.attach_feedback(
        _trace_feedback.TraceFeedback(
            trace_id="tr-1",
            name="domain_quality",
            value=4,
            idempotency_key="external-row-123",
        ),
        mlflow_api=api,
    )

    assert result.feedback_id == "a-existing"
    assert result.created is False


@pytest.mark.unit
def test_get_feedback_view_normalizes_feedback_and_expectation() -> None:
    feedback = SimpleNamespace(
        to_dictionary=lambda: {
            "assessment_id": "a-1",
            "assessment_name": "quality",
            "feedback": {"value": 0.9},
            "rationale": "Grounded",
            "source": {"source_type": "LLM_JUDGE", "source_id": "quality"},
            "metadata": {"model": "judge"},
        }
    )
    expectation = SimpleNamespace(
        to_dictionary=lambda: {
            "assessment_id": "a-2",
            "assessment_name": "expected_answer",
            "expectation": {"value": "approved"},
            "source": {"source_type": "HUMAN", "source_id": "reviewer"},
        }
    )
    trace = SimpleNamespace(
        info=SimpleNamespace(
            trace_id="tr-1",
            tags={"apx.agent": "claims"},
            assessments=[feedback, expectation],
        )
    )

    view = _trace_feedback.get_feedback_view(
        "tr-1", mlflow_api=SimpleNamespace(get_trace=lambda trace_id: trace)
    )

    assert view.trace_id == "tr-1"
    assert view.tags == {"apx.agent": "claims"}
    assert view.assessments == [
        _trace_feedback.TraceAssessment(
            assessment_id="a-1",
            name="quality",
            kind="feedback",
            value=0.9,
            rationale="Grounded",
            source_type="LLM_JUDGE",
            source_id="quality",
            metadata={"model": "judge"},
        ),
        _trace_feedback.TraceAssessment(
            assessment_id="a-2",
            name="expected_answer",
            kind="expectation",
            value="approved",
            rationale=None,
            source_type="HUMAN",
            source_id="reviewer",
            metadata={},
        ),
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trace_id", ""),
        ("name", " "),
        ("value", ["unsupported"]),
    ],
)
def test_attach_feedback_rejects_invalid_input(field: str, value) -> None:
    kwargs = {"trace_id": "tr-1", "name": "quality", "value": True}
    kwargs[field] = value
    with pytest.raises(_trace_feedback.TraceFeedbackError):
        _trace_feedback.attach_feedback(
            _trace_feedback.TraceFeedback(**kwargs),
            mlflow_api=SimpleNamespace(),
        )


@pytest.mark.unit
def test_attach_feedback_rejects_reserved_idempotency_evidence_key() -> None:
    with pytest.raises(
        _trace_feedback.TraceFeedbackError,
        match=_trace_feedback.IDEMPOTENCY_METADATA_KEY,
    ):
        _trace_feedback.attach_feedback(
            _trace_feedback.TraceFeedback(
                trace_id="tr-1",
                name="quality",
                value=True,
                evidence={_trace_feedback.IDEMPOTENCY_METADATA_KEY: "caller-value"},
            ),
            mlflow_api=SimpleNamespace(),
        )


@pytest.mark.unit
def test_get_feedback_view_rejects_missing_trace() -> None:
    api = SimpleNamespace(get_trace=lambda trace_id: None)
    with pytest.raises(_trace_feedback.TraceNotFoundError, match="not found"):
        _trace_feedback.get_feedback_view("tr-missing", mlflow_api=api)


@pytest.mark.unit
def test_default_mlflow_api_reports_unavailable_dependency() -> None:
    with patch.dict("sys.modules", {"mlflow": None}):
        with pytest.raises(
            _trace_feedback.TraceFeedbackUnavailableError,
            match="requires mlflow",
        ):
            _trace_feedback._default_mlflow_api()
