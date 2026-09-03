"""Authenticated per-app HTTP access to MLflow trace feedback."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, NoReturn

from fastapi import APIRouter, HTTPException, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
)

from ._obo import _in_databricks_app, extract_obo_headers
from ._trace_feedback import (
    DEFAULT_SOURCE_ID,
    TraceFeedback,
    TraceFeedbackError,
    TraceFeedbackUnavailableError,
    TraceNotFoundError,
    attach_feedback,
    get_feedback_view,
)


class _TraceFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: StrictStr
    name: StrictStr
    value: StrictBool | StrictInt | StrictFloat | StrictStr
    comment: StrictStr | None = None
    idempotency_key: StrictStr | None = None
    evidence: dict[StrictStr, StrictStr] | None = None


@dataclass(frozen=True)
class _TraceFeedbackContext:
    mlflow_api: Any | None
    source: str


class _OBOTraceFeedbackApi:
    """Feedback API that uses the OBO user token for UC-backed traces.

    Uses mlflow.log_feedback() / mlflow.get_trace() with the OBO credentials
    injected via environment variables. This routes correctly for both legacy
    V2 traces and UC-backed traces (trace:/ format) — the low-level
    DatabricksTracingRestStore.create_assessment only handles V2. Fixes #724.
    """

    def __init__(self, *, host: str, token: str) -> None:
        self._host = host
        self._token = token

    def _obo_env(self) -> dict[str, str]:
        return {
            "DATABRICKS_HOST": self._host,
            "DATABRICKS_TOKEN": self._token,
            "MLFLOW_TRACKING_URI": "databricks",
        }

    def _with_obo_creds(self, fn: Any) -> Any:
        """Run fn with OBO credentials injected as env vars, then restore."""
        import os
        env = self._obo_env()
        old = {k: os.environ.get(k) for k in env}
        try:
            os.environ.update(env)
            return fn()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def get_trace(self, trace_id: str) -> Any:
        import mlflow as _mlflow
        from mlflow.exceptions import MlflowException
        from mlflow.tracking import MlflowClient

        try:
            # Use MlflowClient with explicit tracking URI to avoid mutating
            # global mlflow state. For UC-backed traces the client routes via
            # the correct API path when DATABRICKS_HOST/TOKEN are in env.
            return self._with_obo_creds(
                lambda: MlflowClient(tracking_uri="databricks").get_trace(trace_id)
            )
        except MlflowException as exc:
            if exc.get_http_status_code() == 404:
                return None
            raise

    def log_feedback(self, **kwargs: Any) -> Any:
        import mlflow as _mlflow
        from mlflow.entities import AssessmentSource, AssessmentSourceType

        source = kwargs.get("source")
        if isinstance(source, str):
            source = AssessmentSource(
                source_type=AssessmentSourceType.HUMAN, source_id=source
            )

        # mlflow.log_feedback routes correctly for both V2 and UC-backed traces.
        # Inject OBO credentials via env so it authenticates as the calling user.
        return self._with_obo_creds(
            lambda: _mlflow.log_feedback(
                trace_id=kwargs["trace_id"],
                name=kwargs["name"],
                value=kwargs["value"],
                rationale=kwargs.get("rationale"),
                source=source,
                metadata=kwargs.get("metadata"),
            )
        )


def _request_feedback_context(request: Request) -> _TraceFeedbackContext:
    if not _in_databricks_app():
        return _TraceFeedbackContext(mlflow_api=None, source=DEFAULT_SOURCE_ID)

    obo = extract_obo_headers(headers=request.headers)
    token = obo.get("user_token")
    host = obo.get("workspace_host")
    source = obo.get("user_email") or obo.get("user_id")
    if not token or not host or not source:
        raise HTTPException(
            status_code=401,
            detail="Trace feedback requires an authenticated Databricks Apps user.",
        )
    return _TraceFeedbackContext(
        mlflow_api=_OBOTraceFeedbackApi(host=host, token=token),
        source=source,
    )


def _raise_http_error(exc: Exception) -> NoReturn:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, TraceNotFoundError):
        raise HTTPException(status_code=404, detail="Trace not found.") from exc
    if isinstance(exc, (ImportError, TraceFeedbackUnavailableError)):
        raise HTTPException(
            status_code=503,
            detail="Trace feedback requires the APX eval extra.",
        ) from exc
    if isinstance(exc, TraceFeedbackError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    from mlflow.exceptions import MlflowException

    if isinstance(exc, MlflowException):
        status = exc.get_http_status_code()
        if status == 401:
            raise HTTPException(
                status_code=401,
                detail="MLflow authentication failed.",
            ) from exc
        if status == 403:
            raise HTTPException(status_code=403, detail="Trace access denied.") from exc
        if status == 404:
            raise HTTPException(status_code=404, detail="Trace not found.") from exc
    raise HTTPException(
        status_code=502,
        detail="MLflow trace feedback request failed.",
    ) from exc


def build_trace_feedback_router() -> APIRouter:
    router = APIRouter(prefix="/_apx/feedback", tags=["trace-feedback"])

    @router.post("")
    def post_feedback(body: _TraceFeedbackRequest, request: Request) -> Any:
        try:
            context = _request_feedback_context(request)
            return attach_feedback(
                TraceFeedback(
                    trace_id=body.trace_id,
                    name=body.name,
                    value=body.value,
                    comment=body.comment,
                    source=context.source,
                    idempotency_key=body.idempotency_key,
                    evidence=body.evidence,
                ),
                mlflow_api=context.mlflow_api,
            )
        except Exception as exc:
            _raise_http_error(exc)

    @router.get("/{trace_id:path}")
    def get_feedback(trace_id: str, request: Request) -> Any:
        try:
            context = _request_feedback_context(request)
            return get_feedback_view(trace_id, mlflow_api=context.mlflow_api)
        except Exception as exc:
            _raise_http_error(exc)

    return router
