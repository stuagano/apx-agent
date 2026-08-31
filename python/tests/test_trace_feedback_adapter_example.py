"""Contract tests for the external review adapter example."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_adapter():
    path = Path(__file__).parents[1] / "examples/trace-feedback-adapter/adapter.py"
    assert path.exists(), "trace feedback adapter example is missing"
    spec = spec_from_file_location("trace_feedback_adapter_example", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_adapter_maps_review_record_to_feedback_request() -> None:
    adapter = _load_adapter()
    calls = []

    result = adapter.submit_review(
        {
            "review_id": "review-42",
            "trace_id": "tr-123",
            "feature": "claims_search",
            "label_name": "answer_quality",
            "label_value": 4,
            "rationale": "Correct answer, weak rationale.",
            "screenshot_uri": "s3://reviews/review-42.png",
        },
        allowed_features={"claims_search"},
        post_feedback=lambda path, payload: calls.append((path, payload))
        or {"feedback_id": "a-1"},
    )

    assert result == {"feedback_id": "a-1"}
    assert calls == [
        (
            "/_apx/feedback",
            {
                "trace_id": "tr-123",
                "name": "answer_quality",
                "value": 4,
                "comment": "Correct answer, weak rationale.",
                "idempotency_key": "review-42",
                "evidence": {
                    "external_review_id": "review-42",
                    "feature": "claims_search",
                    "screenshot_uri": "s3://reviews/review-42.png",
                },
            },
        )
    ]


def test_adapter_skips_reviews_outside_allowed_features() -> None:
    adapter = _load_adapter()
    calls = []

    result = adapter.submit_review(
        {
            "review_id": "review-42",
            "trace_id": "tr-123",
            "feature": "general_chat",
            "label_name": "answer_quality",
            "label_value": 2,
        },
        allowed_features={"claims_search"},
        post_feedback=lambda path, payload: calls.append((path, payload)),
    )

    assert result is None
    assert calls == []
