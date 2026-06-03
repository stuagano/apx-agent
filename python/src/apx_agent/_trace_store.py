"""In-process trace ring buffer for the dev-UI Trace detail.

On FEVM / private-link workspaces, ``mlflow.get_trace()`` falls through to
downloading span artifacts from ``*.storage.cloud.databricks.com`` — which is
network-blocked — and HANGS. But ``mlflow.get_trace()`` reads MLflow's
**in-memory buffer first**, so if we snapshot a trace right after it is created
(still in memory, no blob), we can serve it later from this ring buffer.

The store is:
  * **per-process** — a separate replica won't have another replica's traces
    (buffer miss → the route's fail-fast fallback fires).
  * **recent-only** — bounded to the last ``MAX_TRACES`` traces since app start.

Both adapters (``_responses_agent``, ``_chat_agent``) call
``capture_current_trace()`` after each run via the SAME shared helper, so they
capture identically and the serving-conformance contract stays satisfied.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

MAX_TRACES = 50

# Name of the startup self-test span/trace used to materialize the OTel
# provider (see ``install_capture_processor_at_startup``). The dev-UI traces
# list filters traces with this name so the internal warm-up doesn't show as a
# confusing single-span "empty" trace.
WARMUP_SPAN_NAME = "apx.trace_capture.warmup"

_STORE: "OrderedDict[str, list[dict]]" = OrderedDict()
_LOCK = threading.Lock()


def reset() -> None:
    """Clear the buffer (primarily for tests)."""
    with _LOCK:
        _STORE.clear()


def put(trace_id: str, span_dicts: list[dict]) -> None:
    """Store serialized spans for ``trace_id``, evicting the oldest when over
    capacity. Newest entries move to the end; eviction pops from the front."""
    if not trace_id:
        return
    with _LOCK:
        _STORE[trace_id] = span_dicts
        _STORE.move_to_end(trace_id)
        while len(_STORE) > MAX_TRACES:
            _STORE.popitem(last=False)


def get(trace_id: str) -> list[dict] | None:
    """Return the serialized spans for ``trace_id``, or ``None`` on a miss."""
    with _LOCK:
        return _STORE.get(trace_id)


# ---------------------------------------------------------------------------
# Capture mechanism — a SpanProcessor that snapshots the trace at root-span-end
# ---------------------------------------------------------------------------
#
# CAPTURE POINT (empirically verified — see
# ``tests/test_trace_store.py::test_capture_after_real_agent_run``):
#
# The original plan assumed ``mlflow.get_trace()`` reads MLflow's in-memory
# buffer first (so a cheap post-run snapshot would avoid the blob). That premise
# is REFUTED by the installed MLflow source: ``mlflow.tracing.fluent.get_trace``
# goes straight to ``TracingClient().get_trace`` (the tracking store) — which on
# FEVM/private-link is exactly the blocked blob read that hangs. There is no
# in-memory shortcut via ``get_trace``, and post-run the trace has already been
# popped from ``InMemoryTraceManager``.
#
# The robust mechanism is an OpenTelemetry ``SpanProcessor`` whose ``on_end``
# fires for the ROOT span (``span.parent is None``) the instant the trace
# completes — while the COMPLETE trace (AGENT root + autolog LangGraph
# TOOL/LLM/CHAIN children) is still live in ``InMemoryTraceManager``. We read it
# there (no tracking-store / blob access), serialize to plain dicts, and store
# them. This is structurally correct on FEVM because it never touches the
# tracking store. It also covers the STREAMING paths (the dev UI streams, so
# that is the production capture path): the root ``safe_span`` ends when the
# generator is exhausted, so ``on_end`` fires while the trace is still in the
# in-memory manager — empirically verified for both ``streaming`` and
# ``predict_stream`` (see the streaming gating tests).

_INSTALL_LOCK = threading.Lock()


def _capture_root_trace(otel_span: object) -> None:
    """Snapshot the just-completed trace from MLflow's in-memory trace manager
    into the ring store. Called from the SpanProcessor's ``on_end`` for the root
    span only. Never raises (a SpanProcessor exception can surface on the
    request thread)."""
    try:
        import json as _json

        from mlflow.tracing.constant import SpanAttributeKey
        from mlflow.tracing.trace_manager import InMemoryTraceManager

        from ._dev import _serialize_trace_spans  # lazy: avoid import cycle

        attrs = getattr(otel_span, "attributes", None) or {}
        req = attrs.get(SpanAttributeKey.REQUEST_ID)
        # The attribute is OTel-stored as a JSON string (e.g. '"tr-abc"').
        if isinstance(req, str) and req.startswith('"'):
            req = _json.loads(req)
        if not req:
            return
        tm = InMemoryTraceManager.get_instance()
        with tm.get_trace(req) as live:
            if live is None:
                return
            trace = live.to_mlflow_trace()   # materializes a copy
        spans = _serialize_trace_spans(trace)
        if spans:
            put(req, spans)
    except Exception:
        # Best-effort only — tracing must never break a request.
        return


# The capture processor MUST subclass OpenTelemetry's ``SpanProcessor`` for the
# active multi-processor to dispatch ``on_end`` to it (a duck-typed object is
# silently never called). We build the subclass lazily and cache one instance,
# so this module still imports cleanly when OpenTelemetry / MLflow are absent.
_CAPTURE_INSTANCE: Any = None


def _get_capture_processor() -> Any:
    """Return the singleton capture ``SpanProcessor`` (lazily building the OTel
    subclass on first use)."""
    global _CAPTURE_INSTANCE
    if _CAPTURE_INSTANCE is not None:
        return _CAPTURE_INSTANCE
    from opentelemetry.sdk.trace import SpanProcessor

    class _ApxCaptureSpanProcessor(SpanProcessor):
        """Snapshots a completed trace at root-span-end (``span.parent is None``),
        reading the still-live trace from MLflow's in-memory manager."""

        def on_start(self, span, parent_context=None):  # type: ignore[override]
            pass

        def on_end(self, span):  # type: ignore[override]
            if getattr(span, "parent", None) is None:
                _capture_root_trace(span)

        def shutdown(self):  # type: ignore[override]  # pragma: no cover
            pass

        def force_flush(self, timeout_millis: float = 30000) -> bool:  # type: ignore[override]  # pragma: no cover
            return True

    _CAPTURE_INSTANCE = _ApxCaptureSpanProcessor()
    return _CAPTURE_INSTANCE


def ensure_capture_processor() -> bool:
    """Idempotently register the trace-capture ``SpanProcessor`` on the live
    MLflow OTel ``TracerProvider``.

    Self-healing: MLflow rebuilds the provider on tracing enable/disable and
    reseeds on fork, and the provider does not exist until the first span flows.
    A register-once design can silently end up detached. So this is installed at
    startup (``install_capture_processor_at_startup``) AND called at run-start by
    both adapters (a trivial idempotent check, identical in both paths, never
    raises). When the processor is already present this is a no-op — it never
    mutates the active processor set mid-trace (which would break dispatch for
    the in-flight trace). Returns True if the processor is present (installed now
    or already there), False if it could not be installed (e.g. MLflow not
    installed / no SDK provider yet)."""
    try:
        from opentelemetry.sdk.trace import TracerProvider

        from mlflow.tracing.provider import provider as _mlflow_provider

        tp = _mlflow_provider.get()
        if not isinstance(tp, TracerProvider):
            return False

        proc = _get_capture_processor()

        def _already_present(provider: TracerProvider) -> bool:
            # Mirror MLflow's own scan (provider.py) of the active processors.
            active = getattr(provider, "_active_span_processor", None)
            existing = getattr(active, "_span_processors", None) or ()
            return any(p is proc for p in existing)

        if _already_present(tp):
            return True

        with _INSTALL_LOCK:
            if _already_present(tp):  # re-check under lock
                return True
            tp.add_span_processor(proc)
            return True
    except Exception:
        return False


def install_capture_processor_at_startup() -> bool:
    """Materialize the MLflow tracing provider (which does not exist until the
    first span flows) with a tiny warm-up span, then install the capture
    processor — so the VERY FIRST request is captured too. Best-effort; never
    raises. Call once from the app lifespan after autolog setup."""
    try:
        import mlflow
        with mlflow.start_span(WARMUP_SPAN_NAME):
            pass
        return ensure_capture_processor()
    except Exception:
        return False
