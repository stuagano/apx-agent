"""Zero-ops diagnostics tools over system tables.

Three families:
  * DBSQL — ``system.query.history`` (slow / spill / regressions)
  * Jobs  — ``system.lakeflow.job_run_timeline`` (+ Jobs API via jobs_tools)
  * Cost  — ``system.billing.usage`` joined to ``list_prices``
"""

from __future__ import annotations

from typing import Any

from apx_agent import Dependencies, run_sql

Workspace = Dependencies.Workspace


def _clamp_days(lookback_days: int) -> int:
    return max(1, min(int(lookback_days), 30))


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), 50))


def _escape(value: str) -> str:
    return value.strip().replace("'", "''")


# ---------------------------------------------------------------------------
# DBSQL — system.query.history
# ---------------------------------------------------------------------------


def top_expensive_queries(
    lookback_days: int = 7,
    limit: int = 20,
    warehouse_id: str = "",
    executed_by: str = "",
    ws: Workspace = None,
) -> dict[str, Any]:
    """Return the slowest finished DBSQL queries in the lookback window.

    Ranks by ``total_duration_ms``. Optionally filter by warehouse_id or
    executed_by (exact match). Use this first when the user asks what is
    expensive or slow in SQL warehouses.
    """
    days = _clamp_days(lookback_days)
    lim = _clamp_limit(limit)
    filters = [
        f"start_time >= CURRENT_DATE - INTERVAL {days} DAYS",
        "execution_status = 'FINISHED'",
    ]
    if warehouse_id.strip():
        filters.append(f"compute.warehouse_id = '{_escape(warehouse_id)}'")
    if executed_by.strip():
        filters.append(f"executed_by = '{_escape(executed_by)}'")
    where = " AND ".join(filters)
    sql = f"""
SELECT
  statement_id,
  SUBSTRING(statement_text, 1, 200) AS query_preview,
  executed_by,
  start_time,
  total_duration_ms,
  execution_duration_ms,
  compilation_duration_ms,
  result_fetch_duration_ms,
  total_duration_ms - execution_duration_ms - compilation_duration_ms
    AS queue_and_overhead_ms,
  read_rows,
  produced_rows,
  read_bytes,
  spilled_local_bytes,
  compute.warehouse_id AS warehouse_id
FROM system.query.history
WHERE {where}
ORDER BY total_duration_ms DESC
LIMIT {lim}
"""
    rows = run_sql(ws, sql)
    return {"lookback_days": days, "row_count": len(rows), "rows": rows}


def top_spilling_queries(
    lookback_days: int = 7,
    limit: int = 20,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Return queries with the most local spill (memory pressure signal)."""
    days = _clamp_days(lookback_days)
    lim = _clamp_limit(limit)
    sql = f"""
SELECT
  statement_id,
  SUBSTRING(statement_text, 1, 200) AS query_preview,
  executed_by,
  start_time,
  total_duration_ms,
  read_bytes,
  spilled_local_bytes,
  ROUND(spilled_local_bytes * 100.0 / NULLIF(read_bytes, 0), 1) AS spill_pct,
  compute.warehouse_id AS warehouse_id
FROM system.query.history
WHERE start_time >= CURRENT_DATE - INTERVAL {days} DAYS
  AND spilled_local_bytes > 0
ORDER BY spilled_local_bytes DESC
LIMIT {lim}
"""
    rows = run_sql(ws, sql)
    return {"lookback_days": days, "row_count": len(rows), "rows": rows}


def statement_detail(statement_id: str, ws: Workspace = None) -> dict[str, Any]:
    """Time breakdown + metadata for one ``statement_id`` from query history."""
    sid = _escape(statement_id)
    if not sid:
        return {"error": "statement_id is required"}
    sql = f"""
SELECT
  statement_id,
  statement_text,
  executed_by,
  execution_status,
  start_time,
  end_time,
  total_duration_ms,
  compilation_duration_ms,
  execution_duration_ms,
  result_fetch_duration_ms,
  ROUND(compilation_duration_ms * 100.0 / NULLIF(total_duration_ms, 0), 1)
    AS pct_compilation,
  ROUND(execution_duration_ms * 100.0 / NULLIF(total_duration_ms, 0), 1)
    AS pct_execution,
  ROUND(result_fetch_duration_ms * 100.0 / NULLIF(total_duration_ms, 0), 1)
    AS pct_fetch,
  read_rows,
  produced_rows,
  read_bytes,
  spilled_local_bytes,
  compute.warehouse_id AS warehouse_id,
  error_message
FROM system.query.history
WHERE statement_id = '{sid}'
LIMIT 1
"""
    rows = run_sql(ws, sql)
    if not rows:
        return {"statement_id": sid, "found": False, "rows": []}
    return {"statement_id": sid, "found": True, "rows": rows}


def duration_regressions(
    lookback_days: int = 14,
    limit: int = 20,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Compare avg duration last 7d vs prior 7d for recurring query signatures."""
    days = _clamp_days(lookback_days)
    if days < 14:
        days = 14
    lim = _clamp_limit(limit)
    sql = f"""
SELECT
  SUBSTRING(statement_text, 1, 100) AS query_signature,
  COUNT(*) AS executions,
  AVG(CASE WHEN start_time >= CURRENT_DATE - INTERVAL 7 DAYS
           THEN total_duration_ms END) AS avg_ms_last_7d,
  AVG(CASE WHEN start_time BETWEEN CURRENT_DATE - INTERVAL 14 DAYS
                AND CURRENT_DATE - INTERVAL 7 DAYS
           THEN total_duration_ms END) AS avg_ms_prior_7d,
  ROUND(
    (AVG(CASE WHEN start_time >= CURRENT_DATE - INTERVAL 7 DAYS
              THEN total_duration_ms END) -
     AVG(CASE WHEN start_time BETWEEN CURRENT_DATE - INTERVAL 14 DAYS
                   AND CURRENT_DATE - INTERVAL 7 DAYS
              THEN total_duration_ms END)) * 100.0 /
    NULLIF(AVG(CASE WHEN start_time BETWEEN CURRENT_DATE - INTERVAL 14 DAYS
                         AND CURRENT_DATE - INTERVAL 7 DAYS
                    THEN total_duration_ms END), 0),
    1
  ) AS pct_change
FROM system.query.history
WHERE start_time >= CURRENT_DATE - INTERVAL {days} DAYS
  AND execution_status = 'FINISHED'
GROUP BY SUBSTRING(statement_text, 1, 100)
HAVING COUNT(*) >= 5
ORDER BY pct_change DESC NULLS LAST
LIMIT {lim}
"""
    rows = run_sql(ws, sql)
    return {"lookback_days": days, "row_count": len(rows), "rows": rows}


# ---------------------------------------------------------------------------
# Jobs / Spark — system.lakeflow.job_run_timeline (+ jobs.name join)
# ---------------------------------------------------------------------------


def top_expensive_job_runs(
    lookback_days: int = 7,
    limit: int = 20,
    job_id: str = "",
    ws: Workspace = None,
) -> dict[str, Any]:
    """Return the longest Lakeflow job runs in the lookback window.

    Ranks by ``run_duration_seconds``. Optionally filter to one job_id.
    Use this first for Jobs / Spark / Workflow cost-in-time questions.
    """
    days = _clamp_days(lookback_days)
    lim = _clamp_limit(limit)
    filters = [
        f"r.period_start_time >= CURRENT_DATE - INTERVAL {days} DAYS",
        "r.run_duration_seconds IS NOT NULL",
    ]
    if job_id.strip():
        filters.append(f"r.job_id = '{_escape(job_id)}'")
    where = " AND ".join(filters)
    sql = f"""
SELECT
  r.job_id,
  j.name AS job_name,
  r.run_id,
  r.period_start_time,
  r.period_end_time,
  r.run_duration_seconds,
  ROUND(r.run_duration_seconds / 60.0, 1) AS duration_minutes,
  r.result_state,
  r.termination_code,
  j.creator_user_name
FROM system.lakeflow.job_run_timeline r
LEFT JOIN system.lakeflow.jobs j
  ON r.job_id = j.job_id AND j.delete_time IS NULL
WHERE {where}
ORDER BY r.run_duration_seconds DESC
LIMIT {lim}
"""
    rows = run_sql(ws, sql)
    return {"lookback_days": days, "row_count": len(rows), "rows": rows}


def failing_job_runs(
    lookback_days: int = 7,
    limit: int = 20,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Return recent FAILED / TIMEDOUT / CANCELED job runs."""
    days = _clamp_days(lookback_days)
    lim = _clamp_limit(limit)
    sql = f"""
SELECT
  r.job_id,
  j.name AS job_name,
  r.run_id,
  r.period_start_time,
  r.run_duration_seconds,
  r.result_state,
  r.termination_code,
  j.creator_user_name
FROM system.lakeflow.job_run_timeline r
LEFT JOIN system.lakeflow.jobs j
  ON r.job_id = j.job_id AND j.delete_time IS NULL
WHERE r.period_start_time >= CURRENT_DATE - INTERVAL {days} DAYS
  AND r.result_state IN ('FAILED', 'TIMEDOUT', 'CANCELED')
ORDER BY r.period_start_time DESC
LIMIT {lim}
"""
    rows = run_sql(ws, sql)
    return {"lookback_days": days, "row_count": len(rows), "rows": rows}


def job_duration_regressions(
    lookback_days: int = 14,
    limit: int = 20,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Jobs whose avg run duration got slower last 7d vs prior 7d."""
    days = _clamp_days(lookback_days)
    if days < 14:
        days = 14
    lim = _clamp_limit(limit)
    sql = f"""
SELECT
  r.job_id,
  j.name AS job_name,
  COUNT(*) AS runs,
  AVG(CASE WHEN r.period_start_time >= CURRENT_DATE - INTERVAL 7 DAYS
           THEN r.run_duration_seconds END) AS avg_sec_last_7d,
  AVG(CASE WHEN r.period_start_time BETWEEN CURRENT_DATE - INTERVAL 14 DAYS
                AND CURRENT_DATE - INTERVAL 7 DAYS
           THEN r.run_duration_seconds END) AS avg_sec_prior_7d,
  ROUND(
    (AVG(CASE WHEN r.period_start_time >= CURRENT_DATE - INTERVAL 7 DAYS
              THEN r.run_duration_seconds END) -
     AVG(CASE WHEN r.period_start_time BETWEEN CURRENT_DATE - INTERVAL 14 DAYS
                   AND CURRENT_DATE - INTERVAL 7 DAYS
              THEN r.run_duration_seconds END)) * 100.0 /
    NULLIF(AVG(CASE WHEN r.period_start_time BETWEEN CURRENT_DATE - INTERVAL 14 DAYS
                         AND CURRENT_DATE - INTERVAL 7 DAYS
                    THEN r.run_duration_seconds END), 0),
    1
  ) AS pct_change
FROM system.lakeflow.job_run_timeline r
LEFT JOIN system.lakeflow.jobs j
  ON r.job_id = j.job_id AND j.delete_time IS NULL
WHERE r.period_start_time >= CURRENT_DATE - INTERVAL {days} DAYS
  AND r.run_duration_seconds IS NOT NULL
  AND r.result_state IN ('SUCCESS', 'SUCCEEDED')
GROUP BY r.job_id, j.name
HAVING COUNT(*) >= 5
ORDER BY pct_change DESC NULLS LAST
LIMIT {lim}
"""
    rows = run_sql(ws, sql)
    return {"lookback_days": days, "row_count": len(rows), "rows": rows}


def job_success_rates(
    lookback_days: int = 30,
    limit: int = 20,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Jobs with the worst success rate (at least 5 runs in the window)."""
    days = _clamp_days(lookback_days)
    lim = _clamp_limit(limit)
    sql = f"""
SELECT
  r.job_id,
  j.name AS job_name,
  COUNT(*) AS total_runs,
  SUM(CASE WHEN r.result_state IN ('SUCCESS', 'SUCCEEDED') THEN 1 ELSE 0 END)
    AS successful_runs,
  ROUND(
    100.0 * SUM(CASE WHEN r.result_state IN ('SUCCESS', 'SUCCEEDED') THEN 1 ELSE 0 END)
      / COUNT(*),
    2
  ) AS success_rate_pct,
  j.creator_user_name
FROM system.lakeflow.job_run_timeline r
LEFT JOIN system.lakeflow.jobs j
  ON r.job_id = j.job_id AND j.delete_time IS NULL
WHERE r.period_start_time >= CURRENT_DATE - INTERVAL {days} DAYS
GROUP BY r.job_id, j.name, j.creator_user_name
HAVING COUNT(*) >= 5
ORDER BY success_rate_pct ASC
LIMIT {lim}
"""
    rows = run_sql(ws, sql)
    return {"lookback_days": days, "row_count": len(rows), "rows": rows}


# ---------------------------------------------------------------------------
# Cost — system.billing.usage (+ list_prices)
# ---------------------------------------------------------------------------


def top_cost_by_sku(
    lookback_days: int = 7,
    limit: int = 20,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Top SKUs by DBU (and estimated USD when list_prices is available)."""
    days = _clamp_days(lookback_days)
    lim = _clamp_limit(limit)
    sql = f"""
SELECT
  u.sku_name,
  u.usage_unit,
  SUM(u.usage_quantity) AS dbus,
  SUM(u.usage_quantity * COALESCE(lp.pricing_default, 0)) AS usd
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, usage_unit, CAST(pricing.default AS DOUBLE) AS pricing_default
  FROM system.billing.list_prices
  WHERE price_end_time IS NULL
) lp ON u.sku_name = lp.sku_name AND u.usage_unit = lp.usage_unit
WHERE u.usage_date >= CURRENT_DATE - INTERVAL {days} DAYS
GROUP BY u.sku_name, u.usage_unit
ORDER BY dbus DESC
LIMIT {lim}
"""
    rows = run_sql(ws, sql)
    return {"lookback_days": days, "row_count": len(rows), "rows": rows}


def top_cost_by_cluster(
    lookback_days: int = 7,
    limit: int = 20,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Top clusters by DBU from billing usage metadata."""
    days = _clamp_days(lookback_days)
    lim = _clamp_limit(limit)
    sql = f"""
SELECT
  u.usage_metadata.cluster_id AS cluster_id,
  u.usage_metadata.cluster_name AS cluster_name,
  u.sku_name,
  SUM(u.usage_quantity) AS dbus,
  SUM(u.usage_quantity * COALESCE(lp.pricing_default, 0)) AS usd
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, usage_unit, CAST(pricing.default AS DOUBLE) AS pricing_default
  FROM system.billing.list_prices
  WHERE price_end_time IS NULL
) lp ON u.sku_name = lp.sku_name AND u.usage_unit = lp.usage_unit
WHERE u.usage_date >= CURRENT_DATE - INTERVAL {days} DAYS
  AND u.usage_metadata.cluster_id IS NOT NULL
GROUP BY
  u.usage_metadata.cluster_id,
  u.usage_metadata.cluster_name,
  u.sku_name
ORDER BY dbus DESC
LIMIT {lim}
"""
    rows = run_sql(ws, sql)
    return {"lookback_days": days, "row_count": len(rows), "rows": rows}


def top_cost_by_warehouse(
    lookback_days: int = 7,
    limit: int = 20,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Top SQL warehouses by DBU from billing usage metadata."""
    days = _clamp_days(lookback_days)
    lim = _clamp_limit(limit)
    sql = f"""
SELECT
  u.usage_metadata.warehouse_id AS warehouse_id,
  u.sku_name,
  SUM(u.usage_quantity) AS dbus,
  SUM(u.usage_quantity * COALESCE(lp.pricing_default, 0)) AS usd
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, usage_unit, CAST(pricing.default AS DOUBLE) AS pricing_default
  FROM system.billing.list_prices
  WHERE price_end_time IS NULL
) lp ON u.sku_name = lp.sku_name AND u.usage_unit = lp.usage_unit
WHERE u.usage_date >= CURRENT_DATE - INTERVAL {days} DAYS
  AND u.usage_metadata.warehouse_id IS NOT NULL
GROUP BY u.usage_metadata.warehouse_id, u.sku_name
ORDER BY dbus DESC
LIMIT {lim}
"""
    rows = run_sql(ws, sql)
    return {"lookback_days": days, "row_count": len(rows), "rows": rows}


def cost_by_compute_type(
    lookback_days: int = 7,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Roll up DBUs into Jobs / All-Purpose / SQL / Serverless / Other buckets."""
    days = _clamp_days(lookback_days)
    sql = f"""
SELECT
  CASE
    WHEN u.sku_name LIKE '%ALL_PURPOSE%' THEN 'All-Purpose Compute'
    WHEN u.sku_name LIKE '%JOBS%' THEN 'Jobs Compute'
    WHEN u.sku_name LIKE '%SQL%' THEN 'SQL Warehouse'
    WHEN u.sku_name LIKE '%SERVERLESS%' THEN 'Serverless'
    ELSE 'Other'
  END AS compute_type,
  SUM(u.usage_quantity) AS dbus,
  SUM(u.usage_quantity * COALESCE(lp.pricing_default, 0)) AS usd
FROM system.billing.usage u
LEFT JOIN (
  SELECT sku_name, usage_unit, CAST(pricing.default AS DOUBLE) AS pricing_default
  FROM system.billing.list_prices
  WHERE price_end_time IS NULL
) lp ON u.sku_name = lp.sku_name AND u.usage_unit = lp.usage_unit
WHERE u.usage_date >= CURRENT_DATE - INTERVAL {days} DAYS
GROUP BY 1
ORDER BY dbus DESC
"""
    rows = run_sql(ws, sql)
    return {"lookback_days": days, "row_count": len(rows), "rows": rows}


def daily_cost_trend(
    lookback_days: int = 30,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Daily DBU totals with a 7-day moving average."""
    days = _clamp_days(lookback_days)
    if days < 7:
        days = 7
    sql = f"""
SELECT
  usage_date,
  SUM(usage_quantity) AS daily_dbus,
  AVG(SUM(usage_quantity)) OVER (
    ORDER BY usage_date
    ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
  ) AS moving_avg_7d
FROM system.billing.usage
WHERE usage_date >= CURRENT_DATE - INTERVAL {days} DAYS
GROUP BY usage_date
ORDER BY usage_date
"""
    rows = run_sql(ws, sql)
    return {"lookback_days": days, "row_count": len(rows), "rows": rows}
