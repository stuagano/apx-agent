# Trace Feedback Adapter

Connect an existing review backend to the trace-feedback API exposed by an APX
FastAPI application. The example translates one representative review record,
filters it by feature, and submits the resulting payload without introducing a
new review UI or storage layer.

## What it does

- maps an external review ID to APX's best-effort idempotency key
- includes the reviewed feature and external review ID as evidence metadata
- includes an optional screenshot or artifact URI without uploading the artifact
- skips records outside the configured feature set
- delegates HTTP and authentication to the review application's existing
  signed-in user client

## Integrate it

Copy or adapt `submit_review` at the point where the existing backend finalizes
a review:

```python
from adapter import submit_review

result = submit_review(
    review_record,
    allowed_features={"claims_search", "policy_lookup"},
    post_feedback=current_user_feedback_client.post_json,
)
```

`post_json` receives the relative path `/_apx/feedback` and a JSON-compatible
payload. It should return the decoded response body or raise the review
backend's normal HTTP exception.

The client must call the APX application through the Databricks Apps gateway as
the signed-in reviewer. The gateway supplies that user's OBO token and identity.
Do not add a service-principal fallback, persist the OBO token, or place tokens,
workspace hosts, or reviewer identity in the JSON payload.

## Review record

The adapter expects the existing backend to supply:

```python
review_record = {
    "review_id": "review-42",
    "trace_id": "tr-123",
    "feature": "claims_search",
    "label_name": "answer_quality",
    "label_value": 4,
    "rationale": "Correct answer, weak rationale.",
    "screenshot_uri": "s3://reviews/review-42.png",
}
```

Keep the screenshot or multimodal artifact in the existing review system. APX
stores only its reference URI as assessment metadata.

## Verify it

From the repository's `python/` directory:

```bash
uv run --frozen pytest tests/test_trace_feedback_adapter_example.py -q
```

After submission, query the same trace through the authenticated APX app:

```http
GET /_apx/feedback/tr-123
```

Retries with the same `review_id` reuse the matching assessment on a
best-effort basis. Submit a new review ID for a correction.
