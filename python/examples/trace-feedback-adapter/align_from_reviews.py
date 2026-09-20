"""Submit customer-neutral reviews, then document `label align`.

This is not a live MemAlign runner. It posts HUMAN feedback whose
``label_name`` equals the judge name, then prints the command that
turns that tagged cohort into an experimental aligned judge.
"""

from collections.abc import Callable, Collection, Mapping
from typing import Any

from adapter import submit_review

JUDGE_NAME = "domain_quality"

SAMPLE_REVIEWS: tuple[dict[str, Any], ...] = (
    {
        "review_id": "review-pass-1",
        "trace_id": "tr-pass-1",
        "feature": "claims_search",
        "label_name": JUDGE_NAME,
        "label_value": True,
        "rationale": "Cited the right policy and answered the asked question.",
    },
    {
        "review_id": "review-fail-1",
        "trace_id": "tr-fail-1",
        "feature": "claims_search",
        "label_name": JUDGE_NAME,
        "label_value": False,
        "rationale": "Answered a different question than the user asked.",
    },
)


def submit_alignment_reviews(
    reviews: Collection[Mapping[str, Any]],
    *,
    allowed_features: Collection[str],
    post_feedback: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Submit reviews whose ``label_name`` already matches the judge."""
    submitted: list[Mapping[str, Any]] = []
    for review in reviews:
        result = submit_review(
            review,
            allowed_features=allowed_features,
            post_feedback=post_feedback,
        )
        if result is not None:
            submitted.append(result)
    return submitted


def align_command(*, experiment: str, run_id: str, judge_name: str = JUDGE_NAME) -> str:
    return (
        "apx-agent label align \\\n"
        f"  --experiment {experiment} \\\n"
        f"  --judge {judge_name} \\\n"
        f"  --run {run_id} \\\n"
        "  --reflection-model databricks:/databricks-claude-sonnet-4-6 \\\n"
        "  --embedding-model databricks:/databricks-gte-large-en \\\n"
        "  --retrieval-k 5 \\\n"
        f"  --new-version {judge_name}-v2"
    )
