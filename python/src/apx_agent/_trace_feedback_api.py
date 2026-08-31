"""Authenticated per-app HTTP access to MLflow trace feedback."""

from __future__ import annotations

from dataclasses import dataclass
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
    def __init__(self, *, host: str, token: str) -> None:
        from mlflow.store.tracking.databricks_rest_store import (
            DatabricksTracingRestStore,
        )
        from mlflow.utils.rest_utils import MlflowHostCreds

        self._store = DatabricksTracingRestStore(
            lambda: MlflowHostCreds(host=host, token=token)
        )

    def get_trace(self, trace_id: str) -> Any:
        from mlflow.exceptions import MlflowException

        try:
            return self._store.get_trace(trace_id)
        except MlflowException as exc:
            if exc.get_http_status_code() == 404:
                return None
            raise

    def log_feedback(self, **kwargs: Any) -> Any:
        from mlflow.entities import Feedback

        assessment = Feedback(
            trace_id=kwargs["trace_id"],
            name=kwargs["name"],
            value=kwargs["value"],
            rationale=kwargs.get("rationale"),
            source=kwargs.get("source"),
            metadata=kwargs.get("metadata"),
        )
        return self._store.create_assessment(assessment)


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
    if isinstance(exc, TraceNotFoundError):
        raise HTTPException(status_code=404, detail="Trace not found.") from exc
    if isinstance(exc, TraceFeedbackUnavailableError):
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
        context = _request_feedback_context(request)
        try:
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
        context = _request_feedback_context(request)
        try:
            return get_feedback_view(trace_id, mlflow_api=context.mlflow_api)
        except Exception as exc:
            _raise_http_error(exc)

    return router
