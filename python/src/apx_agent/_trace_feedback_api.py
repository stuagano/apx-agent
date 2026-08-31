"""Authenticated per-app HTTP access to MLflow trace feedback."""

from __future__ import annotations

from typing import Any


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
