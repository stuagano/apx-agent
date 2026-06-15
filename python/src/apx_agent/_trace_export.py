"""Trace exporter — dump MLflow traces to a Delta table for analytics.

MLflow's traces table is great for ad-hoc browsing but awkward for the
analytical queries downstream consumers want (cost-per-agent rollups,
tool-call frequency, latency P95 per operation, prompt heatmaps,
audit-event correlation across users). The exporter periodically
copies traces into a Delta table with a flat schema so it can be
joined to ``system.billing.usage``, watchdog violation tables, etc.

Schema (auto-created on first write when ``auto_create=True``):

    CREATE TABLE IF NOT EXISTS {target_table} (
        trace_id        STRING NOT NULL,
        experiment_id   STRING,
        agent_name      STRING,
        operation       STRING,
        status          STRING,
        start_time_ms   BIGINT,
        execution_time_ms BIGINT,
        session_id      STRING,
        user_token_provided BOOLEAN,
        model_endpoint  STRING,
        tool_count      INT,
        watchdog_action STRING,
        watchdog_policy_id STRING,
        tags            STRING,  -- JSON of remaining apx.* attrs
        exported_at     TIMESTAMP
    ) USING DELTA

Pulls apx.* attributes off the root span (or trace-level tags) so
queries can filter / group without parsing nested spans. Idempotent
within a single export run — same trace_id won't be inserted twice in
one call (a MERGE upsert handles concurrent runs).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._sql import run_sql

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


_APX_TAG_KEYS = {
    "apx.agent.name", "apx.operation", "apx.session.id",
    "apx.user.token_provided", "apx.model.endpoint",
    "apx.tool.name", "apx.watchdog.action", "apx.watchdog.policy_id",
}


@dataclass(frozen=True)
class ExportResult:
    """Summary of one export run."""

    target_table: str
    traces_pulled: int
    rows_written: int
    skipped: int  # traces that couldn't be normalised


def _escape_sql(value: str | None) -> str:
    if value is None:
        return "NULL"
    # Spark SQL treats backslash as an escape character by default, so a
    # value ending in a backslash (or containing ``\'``) would otherwise
    # break out of the string literal. Escape backslashes *before* quotes.
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return "'" + escaped + "'"


def _normalise_trace(trace: Any) -> dict[str, Any] | None:
    """Pull the apx.* attributes off a Trace object into a flat dict.

    Handles both the list-of-Trace and DataFrame paths used elsewhere in
    the CLI. Returns ``None`` if the trace doesn't have enough metadata
    to be useful (caller counts it under ``skipped``).
    """
    # DataFrame row case (dict-like)
    if isinstance(trace, dict):
        attrs = trace.get("tags") or trace.get("attributes") or {}
        trace_id = trace.get("trace_id") or trace.get("request_id")
        if not trace_id:
            return None
        return {
            "trace_id": str(trace_id),
            "experiment_id": str(trace.get("experiment_id") or ""),
            "agent_name": attrs.get("apx.agent.name"),
            "operation": attrs.get("apx.operation"),
            "status": str(trace.get("status") or ""),
            "start_time_ms": _coerce_int(trace.get("timestamp_ms") or trace.get("start_time_ms")),
            "execution_time_ms": _coerce_int(trace.get("execution_time_ms")),
            "session_id": attrs.get("apx.session.id"),
            "user_token_provided": _coerce_bool(attrs.get("apx.user.token_provided")),
            "model_endpoint": attrs.get("apx.model.endpoint"),
            "tool_count": _coerce_int(attrs.get("apx.tools.count")),
            "watchdog_action": attrs.get("apx.watchdog.action"),
            "watchdog_policy_id": attrs.get("apx.watchdog.policy_id"),
            "tags": json.dumps({k: v for k, v in attrs.items() if k.startswith("apx.")}, default=str),
        }
    # Trace object case
    info = getattr(trace, "info", None)
    data = getattr(trace, "data", None)
    if info is None:
        return None
    trace_id = getattr(info, "trace_id", None) or getattr(info, "request_id", None)
    if not trace_id:
        return None
    spans = list(getattr(data, "spans", None) or [])
    if not spans:
        # No spans means the trace's payload (artifact download) didn't come
        # through — e.g. a private-link workspace where blob storage is
        # blocked. A tags-only row is not a real export; skip it so the
        # caller counts it under ``skipped`` rather than ``rows_written``.
        return None
    root = _root_span(spans)
    root_attrs = dict(getattr(root, "attributes", {}) or {})
    tags = dict(getattr(info, "tags", {}) or {})
    attrs = {**tags, **root_attrs}  # span attrs take precedence
    return {
        "trace_id": str(trace_id),
        "experiment_id": str(getattr(info, "experiment_id", "") or ""),
        "agent_name": attrs.get("apx.agent.name"),
        "operation": attrs.get("apx.operation"),
        "status": str(getattr(info, "status", "") or ""),
        "start_time_ms": _coerce_int(getattr(info, "timestamp_ms", None)),
        "execution_time_ms": _coerce_int(getattr(info, "execution_time_ms", None)),
        "session_id": attrs.get("apx.session.id"),
        "user_token_provided": _coerce_bool(attrs.get("apx.user.token_provided")),
        "model_endpoint": attrs.get("apx.model.endpoint"),
        "tool_count": _coerce_int(attrs.get("apx.tools.count")),
        "watchdog_action": attrs.get("apx.watchdog.action"),
        "watchdog_policy_id": attrs.get("apx.watchdog.policy_id"),
        "tags": json.dumps({k: v for k, v in attrs.items() if k.startswith("apx.")}, default=str),
    }


def _root_span(spans: list[Any]) -> Any:
    """Return the root span — the one with no parent — falling back to spans[0].

    Span ordering in a Trace is not guaranteed, so reading ``spans[0]`` can
    pull apx.* attributes off a child span. The root is the span whose
    parent id is absent; if none can be identified (older span shapes that
    don't expose a parent attribute), fall back to the first span.
    """
    for span in spans:
        parent = (
            getattr(span, "parent_id", None)
            or getattr(span, "parent_span_id", None)
        )
        if not parent:
            return span
    return spans[0]


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def export_traces(
    *,
    experiment_name: str,
    target_table: str,
    ws: "WorkspaceClient",
    lookback_hours: int = 24,
    warehouse_id: str | None = None,
    auto_create: bool = True,
    max_traces: int = 1000,
) -> ExportResult:
    """Pull MLflow traces and write them to a Delta table.

    Args:
        experiment_name: MLflow experiment name/id to read traces from.
        target_table: Three-part UC name (catalog.schema.table) for the
            destination Delta table.
        ws: ``WorkspaceClient`` for SQL writes.
        lookback_hours: How far back to fetch. Default 24h.
        warehouse_id: Optional SQL warehouse.
        auto_create: When True (default), runs
            ``CREATE TABLE IF NOT EXISTS`` before the first write.
        max_traces: Cap on traces fetched per call. Default 1000.

    Returns:
        ``ExportResult`` with traces pulled / rows written / skipped.
    """
    if target_table.count(".") != 2:
        raise ValueError(
            f"target_table must be a three-part UC name; got {target_table!r}"
        )

    try:
        import mlflow
    except ImportError as e:  # pragma: no cover — exercised only without extra
        raise ImportError(
            "export_traces requires mlflow. Install with: pip install 'apx-agent[eval]'"
        ) from e

    if auto_create:
        ddl = (
            f"CREATE TABLE IF NOT EXISTS {target_table} ("
            f"  trace_id STRING NOT NULL,"
            f"  experiment_id STRING,"
            f"  agent_name STRING,"
            f"  operation STRING,"
            f"  status STRING,"
            f"  start_time_ms BIGINT,"
            f"  execution_time_ms BIGINT,"
            f"  session_id STRING,"
            f"  user_token_provided BOOLEAN,"
            f"  model_endpoint STRING,"
            f"  tool_count INT,"
            f"  watchdog_action STRING,"
            f"  watchdog_policy_id STRING,"
            f"  tags STRING,"
            f"  exported_at TIMESTAMP"
            f") USING DELTA"
        )
        try:
            run_sql(ws, ddl, warehouse_id=warehouse_id)
        except Exception as e:
            logger.warning(
                "export_traces: CREATE TABLE IF NOT EXISTS %s failed: %s — "
                "subsequent inserts may fail.",
                target_table, e,
            )

    from ._mlflow_tracing import search_traces_for_experiment

    try:
        traces_raw = search_traces_for_experiment(
            experiment_name,
            max_results=max_traces,
        )
    except Exception as e:
        raise RuntimeError(f"mlflow.search_traces failed: {e}") from e

    # Handle both DataFrame and list returns
    if hasattr(traces_raw, "to_dict"):
        try:
            traces_iter: Any = traces_raw.to_dict(orient="records")
        except Exception:
            traces_iter = []
    else:
        traces_iter = traces_raw or []

    rows: list[dict[str, Any]] = []
    skipped = 0
    for t in traces_iter:
        norm = _normalise_trace(t)
        if norm is None:
            skipped += 1
            continue
        rows.append(norm)

    if not rows:
        return ExportResult(target_table=target_table, traces_pulled=skipped, rows_written=0, skipped=skipped)

    # Build a single MERGE statement so concurrent exports don't double-write.
    # Source is a VALUES clause inline; trace_id is the merge key. Every
    # string value (notably the request-controlled ``session_id``) is run
    # through ``_escape_sql``, which now escapes backslashes *and* quotes so
    # a backslash- or quote-bearing value can't break out of the literal.
    # Numeric / boolean columns are coerced to int/bool above (never
    # attacker strings) so they stay inline as typed literals.
    now = time.time()
    value_tuples = []
    for r in rows:
        value_tuples.append(
            "("
            f"{_escape_sql(r['trace_id'])}, "
            f"{_escape_sql(r['experiment_id'])}, "
            f"{_escape_sql(r['agent_name'])}, "
            f"{_escape_sql(r['operation'])}, "
            f"{_escape_sql(r['status'])}, "
            f"{r['start_time_ms'] if r['start_time_ms'] is not None else 'NULL'}, "
            f"{r['execution_time_ms'] if r['execution_time_ms'] is not None else 'NULL'}, "
            f"{_escape_sql(r['session_id'])}, "
            f"{('TRUE' if r['user_token_provided'] else 'FALSE') if r['user_token_provided'] is not None else 'NULL'}, "
            f"{_escape_sql(r['model_endpoint'])}, "
            f"{r['tool_count'] if r['tool_count'] is not None else 'NULL'}, "
            f"{_escape_sql(r['watchdog_action'])}, "
            f"{_escape_sql(r['watchdog_policy_id'])}, "
            f"{_escape_sql(r['tags'])}, "
            f"CAST({now} AS TIMESTAMP)"
            ")"
        )
    sql = (
        f"MERGE INTO {target_table} AS target "
        f"USING (SELECT * FROM VALUES "
        + ", ".join(value_tuples)
        + " AS src("
        "  trace_id, experiment_id, agent_name, operation, status,"
        "  start_time_ms, execution_time_ms, session_id, user_token_provided,"
        "  model_endpoint, tool_count, watchdog_action, watchdog_policy_id,"
        "  tags, exported_at"
        ")) src "
        "ON target.trace_id = src.trace_id "
        "WHEN MATCHED THEN UPDATE SET * "
        "WHEN NOT MATCHED THEN INSERT *"
    )
    try:
        run_sql(ws, sql, warehouse_id=warehouse_id)
        rows_written = len(rows)
    except Exception as e:
        logger.warning(
            "export_traces: MERGE INTO %s failed: %s — no rows written.",
            target_table, e,
        )
        rows_written = 0

    return ExportResult(
        target_table=target_table,
        traces_pulled=len(rows) + skipped,
        rows_written=rows_written,
        skipped=skipped,
    )
