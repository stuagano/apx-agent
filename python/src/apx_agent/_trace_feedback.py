"""Trace-linked human feedback over MLflow 3.14 assessment APIs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


FeedbackValue = bool | int | float | str
AssessmentValue = FeedbackValue | dict[str, FeedbackValue] | list[FeedbackValue] | None

IDEMPOTENCY_METADATA_KEY = "apx.feedback.idempotency_key"
DEFAULT_SOURCE_ID = "apx.trace_feedback"


class TraceFeedbackError(ValueError):
    """Raised when trace feedback cannot be validated or read."""


class TraceNotFoundError(TraceFeedbackError):
    """Raised when an MLflow trace does not exist."""


class TraceFeedbackUnavailableError(TraceFeedbackError):
    """Raised when optional MLflow feedback support is unavailable."""


@dataclass(frozen=True)
class TraceFeedback:
    trace_id: str
    name: str
    value: FeedbackValue
    comment: str | None = None
    source: str | None = None
    idempotency_key: str | None = None
    evidence: dict[str, str] | None = None


@dataclass(frozen=True)
class TraceFeedbackResult:
    trace_id: str
    feedback_id: str | None
    name: str
    created: bool


@dataclass(frozen=True)
class TraceAssessment:
    assessment_id: str | None
    name: str | None
    kind: str
    value: AssessmentValue
    rationale: str | None
    source_type: str | None
    source_id: str | None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TraceFeedbackView:
    trace_id: str
    tags: dict[str, str]
    assessments: list[TraceAssessment]


class _DefaultMlflowApi:
    """Thin wrapper around mlflow that returns assessments with metadata populated.

    mlflow.get_trace() returns Feedback objects whose .to_dictionary() does not
    include the metadata field, so idempotency_key lookups fail. Using
    mlflow.MlflowClient().get_trace() and converting via to_dictionary() preserves
    the full assessment shape including metadata.
    """

    def get_trace(self, trace_id: str) -> Any:
        """Return trace info with assessments including metadata.

        Uses search_traces (DataFrame path) instead of get_trace because
        search_traces returns assessments as dicts with the full metadata field
        populated, whereas get_trace returns Feedback objects that omit metadata.
        """
        import mlflow
        from types import SimpleNamespace

        try:
            df = mlflow.search_traces(
                filter_string=f"attributes.trace_id = '{trace_id}'",
                max_results=1,
            )
        except Exception:
            # Fallback: search_traces may require a warehouse ID not set locally
            df = None

        if df is not None and len(df):
            row = df.iloc[0]
            info = SimpleNamespace(
                trace_id=str(row.get("trace_id", trace_id)),
                tags=dict(row.get("tags") or {}),
                assessments=list(row.get("assessments") or []),
            )
            return SimpleNamespace(info=info)

        # Last resort: get_trace without metadata (idempotency won't match but
        # won't crash either — worst case creates a duplicate)
        t = mlflow.get_trace(trace_id)
        if t is None:
            return None
        info = SimpleNamespace(
            trace_id=t.info.trace_id,
            tags=dict(t.info.tags or {}),
            assessments=[],  # assessments without metadata are useless for idempotency
        )
        return SimpleNamespace(info=info)

    def log_feedback(self, **kwargs: Any) -> Any:
        import mlflow
        from mlflow.entities import AssessmentSource, AssessmentSourceType

        source = kwargs.get("source")
        if isinstance(source, str):
            source = AssessmentSource(
                source_type=AssessmentSourceType.HUMAN, source_id=source
            )
        return mlflow.log_feedback(
            trace_id=kwargs["trace_id"],
            name=kwargs["name"],
            value=kwargs["value"],
            rationale=kwargs.get("rationale"),
            source=source,
            metadata=kwargs.get("metadata"),
        )


def _default_mlflow_api() -> Any:
    try:
        import mlflow as _mlflow  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise TraceFeedbackUnavailableError(
            "trace feedback requires mlflow; install 'apx-agent[eval]'"
        ) from exc
    return _DefaultMlflowApi()


def _validate_feedback(feedback: TraceFeedback) -> None:
    if not feedback.trace_id.strip():
        raise TraceFeedbackError("trace_id must be non-empty")
    if not feedback.name.strip():
        raise TraceFeedbackError("name must be non-empty")
    if type(feedback.value) not in (bool, int, float, str):
        raise TraceFeedbackError("value must be bool, int, float, or str")
    if feedback.source is not None and not feedback.source.strip():
        raise TraceFeedbackError("source must be non-empty when provided")
    if feedback.idempotency_key is not None and not feedback.idempotency_key.strip():
        raise TraceFeedbackError("idempotency_key must be non-empty when provided")
    if feedback.evidence is not None and not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in feedback.evidence.items()
    ):
        raise TraceFeedbackError("evidence keys and values must be strings")
    if feedback.evidence is not None and IDEMPOTENCY_METADATA_KEY in feedback.evidence:
        raise TraceFeedbackError(
            f"evidence key {IDEMPOTENCY_METADATA_KEY!r} is reserved"
        )


def _assessment_dict(assessment: Any) -> dict[str, Any]:
    if isinstance(assessment, dict):
        return assessment
    value = assessment.to_dictionary()
    if isinstance(value, dict):
        return value
    raise TraceFeedbackError("MLflow returned an unsupported assessment shape")


def _normalize_assessment(assessment: Any) -> TraceAssessment:
    raw = _assessment_dict(assessment)
    source = raw.get("source")
    source = source if isinstance(source, dict) else {}
    metadata = raw.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    kind = "unknown"
    value: AssessmentValue = None
    for candidate in ("feedback", "expectation"):
        payload = raw.get(candidate)
        if isinstance(payload, dict):
            kind = candidate
            value = payload.get("value")
            break

    return TraceAssessment(
        assessment_id=raw.get("assessment_id"),
        name=raw.get("assessment_name") or raw.get("name"),
        kind=kind,
        value=value,
        rationale=raw.get("rationale"),
        source_type=source.get("source_type"),
        source_id=source.get("source_id"),
        metadata={str(key): str(item) for key, item in metadata.items()},
    )


def get_feedback_view(trace_id: str, *, mlflow_api: Any = None) -> TraceFeedbackView:
    """Return tags and normalized assessments for one MLflow trace."""
    trace_id = trace_id.strip()
    if not trace_id:
        raise TraceFeedbackError("trace_id must be non-empty")
    api = mlflow_api or _default_mlflow_api()
    trace = api.get_trace(trace_id)
    if trace is None:
        raise TraceNotFoundError(f"MLflow trace {trace_id!r} not found")
    info = trace.info
    return TraceFeedbackView(
        trace_id=str(info.trace_id),
        tags=dict(info.tags or {}),
        assessments=[_normalize_assessment(item) for item in info.assessments or []],
    )


def attach_feedback(
    feedback: TraceFeedback, *, mlflow_api: Any = None
) -> TraceFeedbackResult:
    """Attach HUMAN feedback to a trace with best-effort replay protection."""
    _validate_feedback(feedback)
    api = mlflow_api or _default_mlflow_api()
    trace_id = feedback.trace_id.strip()
    name = feedback.name.strip()

    if feedback.idempotency_key is not None:
        idempotency_key = feedback.idempotency_key.strip()
        existing = get_feedback_view(trace_id, mlflow_api=api)
        for assessment in existing.assessments:
            if assessment.metadata.get(IDEMPOTENCY_METADATA_KEY) == idempotency_key:
                return TraceFeedbackResult(
                    trace_id=trace_id,
                    feedback_id=assessment.assessment_id,
                    name=name,
                    created=False,
                )

    from mlflow.entities import AssessmentSource, AssessmentSourceType

    metadata = dict(feedback.evidence or {})
    if feedback.idempotency_key is not None:
        metadata[IDEMPOTENCY_METADATA_KEY] = feedback.idempotency_key.strip()
    created = api.log_feedback(
        trace_id=trace_id,
        name=name,
        value=feedback.value,
        rationale=feedback.comment,
        source=AssessmentSource(
            source_type=AssessmentSourceType.HUMAN,
            source_id=(feedback.source or DEFAULT_SOURCE_ID).strip(),
        ),
        metadata=metadata or None,
    )
    return TraceFeedbackResult(
        trace_id=trace_id,
        feedback_id=created.assessment_id,
        name=name,
        created=True,
    )
