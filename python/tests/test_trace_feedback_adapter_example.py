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


def _load_align_from_reviews():
    import sys

    path = Path(__file__).parents[1] / "examples/trace-feedback-adapter/align_from_reviews.py"
    assert path.exists(), "align_from_reviews example is missing"
    example_dir = str(path.parent)
    if example_dir not in sys.path:
        sys.path.insert(0, example_dir)
    spec = spec_from_file_location("align_from_reviews_example", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_align_from_reviews_payload_name_matches_judge() -> None:
    module = _load_align_from_reviews()
    calls = []
    submitted = module.submit_alignment_reviews(
        module.SAMPLE_REVIEWS,
        allowed_features={"claims_search"},
        post_feedback=lambda path, payload: calls.append((path, payload)) or {"ok": True},
    )
    assert submitted == [{"ok": True}, {"ok": True}]
    assert {payload["name"] for _, payload in calls} == {module.JUDGE_NAME}
    assert {payload["value"] for _, payload in calls} == {True, False}
    assert all(isinstance(payload["comment"], str) and payload["comment"] for _, payload in calls)
    command = module.align_command(experiment="123", run_id="domain_quality-run")
    assert "--judge domain_quality" in command
    assert "--new-version domain_quality-v2" in command

