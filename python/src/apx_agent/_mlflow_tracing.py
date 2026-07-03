"""MLflow tracing helpers for the compile / ChatAgent / /invocations path.

Replaces the bucket-3 custom ``_trace.py`` machinery for the supported runtime
with native MLflow spans. Traces emitted via these helpers flow into:

  * The MLflow UI (any tracking URI — Databricks Managed MLflow, OSS, local)
  * Databricks Agent Evaluation (trace-based scorers)
  * Databricks AI Playground / Review App
  * BigQuery Agent Analytics (when configured)

Design principles:

  1. **No-op when MLflow is missing.** ``safe_span`` returns a null context
     manager so the compile path works without the ``eval`` extra installed.
     Users opt into MLflow tracing by installing it.

  2. **No autolog by default.** ``mlflow.langchain.autolog()`` adds ~30s
     overhead per run (it logs the model itself, not just traces — verified
     scar tissue from the customer's hand-rolled pipeline). Use selective
     spans by default; expose ``enable_langchain_autolog()`` for users who
     want deep auto-instrumentation and can pay the cost.

  3. **No forced tracking URI.** We never call ``mlflow.set_tracking_uri``.
     The user (or Databricks env) configures the destination; we just emit.

  4. **Errors during tracing must not break requests.** Every helper catches
     and logs; the agent runs even if span emission fails.

Encoded conventions:

  * Top-level ``ChatAgent.predict`` / ``predict_stream`` → span_type=AGENT
  * ``/invocations`` route handler → span_type=CHAIN
  * Compiled graph invocation → span_type=CHAIN
  * Individual tool dispatch → span_type=TOOL
"""

from __future__ import annotations

import collections
import contextlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Export-error buffer — read by /_apx/probe/checks for the mlflow_export check
# ---------------------------------------------------------------------------

# Bounded deque of recent export-failure log messages.  The probe reads from
# this to detect the FEVM/private-link blob-storage footgun without injecting
# test traces.
_mlflow_export_errors: collections.deque[str] = collections.deque(maxlen=20)


class _ExportErrorCapture(logging.Handler):
    """Capture WARNING+ messages from the MLflow v3 exporter into a deque."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            _mlflow_export_errors.append(self.format(record))


def _install_export_error_handler() -> None:
    """Attach the capture handler to the MLflow v3 tracing exporter logger.

    Called once at module import.  Safe to call multiple times — the guard
    skips reinstall if our handler is already present.
    """
    target = logging.getLogger("mlflow.tracing.export.mlflow_v3")
    for h in target.handlers:
        if isinstance(h, _ExportErrorCapture):
            return
    handler = _ExportErrorCapture()
    handler.setFormatter(logging.Formatter("%(message)s"))
    target.addHandler(handler)
    # Ensure WARNING-level messages propagate even if the root logger is higher.
    if target.level == logging.NOTSET or target.level > logging.WARNING:
        target.setLevel(logging.WARNING)


_install_export_error_handler()


# ---------------------------------------------------------------------------
# Availability + setup
# ---------------------------------------------------------------------------


def is_mlflow_available() -> bool:
    """Return True iff MLflow's tracing API is importable."""
    try:
        import mlflow  # noqa: F401
    except ImportError:
        return False
    return True


def _resolve_experiment_id(experiment: str) -> str:
    """Resolve an experiment name/path (or id) to an experiment id.

    Raises ``ValueError`` if ``experiment`` is neither a known experiment name
    nor a valid experiment id, so a mistyped/missing experiment surfaces as an
    error rather than mis-reporting a failed read as "no traces".
    """
    import mlflow

    by_name = mlflow.get_experiment_by_name(experiment)
    if by_name is not None:
        return by_name.experiment_id
    # Not a known name — maybe it's already an id. Validate it exists.
    try:
        exp = mlflow.get_experiment(experiment)
    except Exception:
        exp = None
    if exp is not None:
        return exp.experiment_id
    raise ValueError(
        f"MLflow experiment {experiment!r} not found "
        "(neither a known experiment name nor a valid experiment id)"
    )


def search_traces_for_experiment(experiment: str, **kwargs: Any) -> Any:
    """Search MLflow traces scoped to one experiment, given its name or id.

    Resolves ``experiment`` to an id and calls
    ``mlflow.search_traces(locations=[id], **kwargs)``. The ``locations``
    selector matters: mlflow 3.x ``search_traces`` has **no**
    ``experiment_names`` parameter (passing it raises ``TypeError``), and
    ``experiment_ids`` is deprecated in favour of ``locations``. Pass-through
    kwargs (``filter_string``, ``max_results``, ``include_spans``, …) are
    forwarded unchanged, so callers keep full control of the query.
    """
    import mlflow

    exp_id = _resolve_experiment_id(experiment)
    return mlflow.search_traces(locations=[exp_id], **kwargs)


def enable_langchain_autolog() -> bool:
    """Turn on ``mlflow.langchain.autolog()`` for deep auto-tracing.

    This auto-instruments every LangChain / LangGraph call (LLM invocations,
    tool calls, graph nodes) into MLflow traces. Costs ~30s overhead per run
    in some configurations (model logging side-effect), so it's opt-in.

    Returns True if autolog was enabled; False if MLflow isn't installed.
    """
    try:
        import mlflow.langchain as _lc
    except ImportError:
        logger.warning(
            "Cannot enable LangChain autolog: mlflow not installed. "
            "Install apx-agent[eval]."
        )
        return False
    try:
        _lc.autolog()
        logger.info("Enabled mlflow.langchain.autolog() — deep LangGraph tracing")
        return True
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Failed to enable LangChain autolog: %s", exc)
        return False


def autolog_if_env() -> None:
    """If ``APX_AGENT_MLFLOW_AUTOLOG=1`` is set in the environment, enable
    LangChain autolog. Call this once at app startup.

    ``apx-agent run`` sets this variable by default (via ``os.environ.setdefault``)
    so the dev loop's Trace panel gets per-tool + per-LLM spans without the
    user opting in. Deploy runtimes reach this with the env unset and stay
    on the cheaper boot path. Set ``APX_AGENT_MLFLOW_AUTOLOG=0`` to force off
    in either case.
    """
    _autolog_raw = os.environ.get("APX_AGENT_MLFLOW_AUTOLOG")
    if _autolog_raw and _autolog_raw.strip().lower() in ("1", "true", "yes"):
        enable_langchain_autolog()


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def safe_span(
    name: str,
    *,
    span_type: str = "UNKNOWN",
    inputs: Any = None,
    attributes: dict[str, Any] | None = None,
):
    """Context manager wrapping ``mlflow.start_span`` with belt-and-suspenders
    safety.

    No-ops if:
      * MLflow isn't installed
      * ``mlflow.start_span`` raises (e.g. tracking misconfigured)

    The wrapped block always runs to completion regardless of tracing state —
    a request must never fail because tracing failed.

    Args:
        name: Human-readable span name (e.g. ``"ChatAgent.predict"``).
        span_type: One of MLflow's SpanType strings — ``"AGENT"``, ``"CHAIN"``,
            ``"TOOL"``, ``"LLM"``, ``"RETRIEVER"``, etc. Defaults to
            ``"UNKNOWN"`` if a more specific type isn't obvious.
        inputs: Optional input payload (must be JSON-serializable; MLflow
            stringifies).
        attributes: Optional dict of extra attributes for the span.

    Usage::

        with safe_span("ChatAgent.predict", span_type="AGENT",
                       attributes={"message_count": 3}) as span:
            result = do_work()
            if span is not None:
                span.set_outputs(result)
    """
    if not is_mlflow_available():
        yield None
        return

    import mlflow

    try:
        cm = mlflow.start_span(name=name, span_type=span_type)
    except Exception as exc:
        logger.debug("mlflow.start_span failed (continuing without trace): %s", exc)
        yield None
        return

    try:
        with cm as span:
            try:
                if inputs is not None and span is not None:
                    span.set_inputs(inputs)
                if attributes and span is not None:
                    for k, v in attributes.items():
                        span.set_attribute(k, v)
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug("Failed to set span inputs/attrs: %s", exc)
            yield span
    except Exception as exc:
        logger.debug("Exception during traced block (re-raising): %s", exc)
        raise


def set_span_outputs(span: Any, outputs: Any) -> None:
    """Safely call ``span.set_outputs(...)`` — no-op if span is None or fails."""
    if span is None:
        return
    try:
        span.set_outputs(outputs)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("Failed to set span outputs: %s", exc)


def set_span_attribute(span: Any, key: str, value: Any) -> None:
    """Safely set a single attribute on a span — no-op if span is None."""
    if span is None:
        return
    try:
        span.set_attribute(key, value)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("Failed to set span attribute %r: %s", key, exc)


def set_trace_tags(tags: dict[str, str]) -> None:
    """Safely set trace-level tags on the ACTIVE trace.

    Trace tags (unlike span attributes) are readable from a metadata-only
    ``search_traces(include_spans=False)`` — which is how ``canary analyze``
    partitions traffic. No-op when MLflow is missing, no trace is active, or
    the write fails: a request must never fail because tagging failed.
    """
    if not tags or not is_mlflow_available():
        return
    import mlflow

    try:
        mlflow.update_current_trace(tags=tags)
    except Exception as exc:
        logger.debug("Failed to set trace tags %r: %s", sorted(tags), exc)


def emit_progress(message: str, **attributes: Any) -> None:
    """Record a progress marker as an event on the current active MLflow span.

    Surfaces tool progress (e.g. a SQL-warehouse cold-start) in the trace
    without streaming. No-ops safely when MLflow is absent or there is no
    active span — never raises into the caller.
    """
    try:
        from mlflow.entities import SpanEvent

        span = current_active_span()
        if span is None:
            return
        attrs: dict[str, Any] = {"message": message}
        attrs.update({k: str(v) for k, v in attributes.items()})
        span.add_event(SpanEvent(name="apx.progress", attributes=attrs))
    except Exception:  # pragma: no cover — tracing must never break a tool
        return


def current_active_span() -> Any:
    """Return the currently active MLflow span, or ``None``.

    Used by callbacks that fire inside an outer span (e.g. LangChain's
    autolog spans, or apx-agent's own predict span) to attach audit
    attributes without owning the span lifecycle themselves.

    Returns ``None`` when MLflow isn't installed, no span is active,
    or the lookup raises — never raises itself.
    """
    try:
        import mlflow
        return mlflow.get_current_active_span()
    except Exception:
        return None
