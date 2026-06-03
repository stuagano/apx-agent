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

MAX_TRACES = 50

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


def capture_current_trace() -> str | None:
    """Best-effort: snapshot the just-finished trace from MLflow's in-memory
    buffer into the ring store (no blob fetch). Returns the trace_id or None.
    Never raises into the caller.

    CAPTURE POINT (empirically verified — see
    ``tests/test_trace_store.py::test_capture_after_real_agent_run``):
    this MUST be called AFTER the adapter's outermost ``safe_span`` block has
    EXITED. While that root span is still open the trace is not finalized, and
    ``get_last_active_trace_id()`` / ``get_trace`` would return an incomplete
    trace (missing the autolog LangGraph TOOL/LLM child spans). Because
    ``safe_span`` opens the root span first, the autolog spans nest as children
    of the SAME trace — so once the root closes, this captures one trace
    carrying both the AGENT root and the TOOL/LLM children, which is exactly
    the trace the dev UI's ``finalizeTrace`` fetches.
    """
    try:
        import mlflow
        from ._dev import _serialize_trace_spans  # lazy: avoid import cycle
        trace_id = mlflow.get_last_active_trace_id()
        if not trace_id:
            return None
        trace = mlflow.get_trace(trace_id, silent=True)   # in-memory first
        spans = _serialize_trace_spans(trace) if trace is not None else []
        if spans:
            put(trace_id, spans)
        return trace_id
    except Exception:
        return None
