"""Adapt an existing review record to the APX trace-feedback endpoint."""

from collections.abc import Callable, Collection, Mapping
from typing import Any


def submit_review(
    review: Mapping[str, Any],
    *,
    allowed_features: Collection[str],
    post_feedback: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    feature = review["feature"]
    if feature not in allowed_features:
        return None

    review_id = review["review_id"]
    evidence = {
        "external_review_id": review_id,
        "feature": feature,
    }
    if screenshot_uri := review.get("screenshot_uri"):
        evidence["screenshot_uri"] = screenshot_uri

    return post_feedback(
        "/_apx/feedback",
        {
            "trace_id": review["trace_id"],
            "name": review["label_name"],
            "value": review["label_value"],
            "comment": review.get("rationale"),
            "idempotency_key": review_id,
            "evidence": evidence,
        },
    )
