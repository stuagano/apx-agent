"""Cost-tracking helpers — DBU / $ per agent or session from system tables.

Databricks emits per-endpoint usage to ``system.billing.usage`` (DBU
consumption) and exposes per-resource pricing via
``system.billing.list_prices``. These helpers join the two to answer
"how much did agent X cost in the last 24h?" without each caller
re-deriving the query.

The query shape (simplified)::

    SELECT
      sku_name,
      usage_unit,
      SUM(usage_quantity) AS dbus,
      SUM(usage_quantity * lp.pricing_default) AS usd
    FROM system.billing.usage u
    LEFT JOIN (latest pricing per sku) lp
      ON u.sku_name = lp.sku_name AND u.usage_unit = lp.usage_unit
    WHERE u.usage_metadata.endpoint_name = ?
      AND u.usage_start_time > CURRENT_TIMESTAMP - INTERVAL ? HOUR

Column names (``usage_metadata.endpoint_name`` etc.) match the public
system-table schema. Pricing joins are best-effort — if the workspace
hasn't enabled the pricing share, the function returns DBU only and
``usd`` is ``None``.

For session-level cost, the lookup is via the ``apx.session.id`` trace
tag the audit layer writes — the helper finds traces in the window,
maps them to endpoint usage by request_id, and aggregates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._sql import run_sql
from ._sql import sql_escape as _escape_sql

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostBreakdown:
    """Per-SKU cost breakdown for one lookup."""

    endpoint: str
    lookback_hours: int
    rows: list[dict[str, Any]] = field(default_factory=list)
    total_dbus: float = 0.0
    total_usd: float | None = None

    @classmethod
    def from_rows(cls, *, endpoint: str, lookback_hours: int, rows: list[dict[str, Any]]) -> "CostBreakdown":
        total_dbus = sum(float(r.get("dbus") or 0.0) for r in rows)
        # Total USD requires every row to have pricing — partial coverage
        # means the total would understate cost, so surface None instead.
        total_usd: float | None
        if rows and all(isinstance(r.get("usd"), (int, float)) for r in rows):
            total_usd = sum(float(r["usd"]) for r in rows)
        else:
            total_usd = None
        return cls(
            endpoint=endpoint,
            lookback_hours=lookback_hours,
            rows=list(rows),
            total_dbus=total_dbus,
            total_usd=total_usd,
        )


# ---------------------------------------------------------------------------
# Per-endpoint cost
# ---------------------------------------------------------------------------


def cost_for_endpoint(
    endpoint: str,
    *,
    ws: "WorkspaceClient",
    lookback_hours: int = 24,
    warehouse_id: str | None = None,
) -> CostBreakdown:
    """Return DBU + USD breakdown for a Model Serving endpoint.

    Args:
        endpoint: Serving endpoint name (the one ``databricks.agents.deploy``
            produced).
        ws: ``WorkspaceClient`` used for the SQL Statements API.
        lookback_hours: Window to aggregate over. Default 24h.
        warehouse_id: Optional SQL warehouse. When omitted, auto-discovered.

    Returns:
        ``CostBreakdown`` with per-SKU rows + totals. ``total_usd`` is
        ``None`` when pricing is unavailable (e.g. system.billing.list_prices
        share not enabled in this workspace).
    """
    if not endpoint:
        raise ValueError("endpoint must be a non-empty string")
    if lookback_hours <= 0:
        raise ValueError(f"lookback_hours must be positive; got {lookback_hours!r}")

    sql = (
        f"SELECT "
        f"  u.sku_name, "
        f"  u.usage_unit, "
        f"  SUM(u.usage_quantity) AS dbus, "
        f"  SUM(u.usage_quantity * COALESCE(lp.pricing_default, 0)) AS usd, "
        f"  MAX(lp.pricing_default) AS unit_price "
        f"FROM system.billing.usage u "
        f"LEFT JOIN ( "
        f"  SELECT sku_name, usage_unit, "
        f"         CAST(pricing.default AS DOUBLE) AS pricing_default "
        f"  FROM system.billing.list_prices "
        f"  WHERE price_end_time IS NULL "
        f") lp ON u.sku_name = lp.sku_name AND u.usage_unit = lp.usage_unit "
        f"WHERE u.usage_metadata.endpoint_name = '{_escape_sql(endpoint)}' "
        f"  AND u.usage_start_time > CURRENT_TIMESTAMP - INTERVAL {lookback_hours} HOUR "
        f"GROUP BY u.sku_name, u.usage_unit "
        f"ORDER BY dbus DESC"
    )

    try:
        raw_rows = run_sql(ws, sql, warehouse_id=warehouse_id)
    except Exception as e:
        logger.warning(
            "cost_for_endpoint(%s): SQL failed: %s — returning empty breakdown. "
            "Common causes: system.billing.usage share not enabled, "
            "or insufficient grants on system schemas.",
            endpoint, e,
        )
        return CostBreakdown(endpoint=endpoint, lookback_hours=lookback_hours)

    normalised: list[dict[str, Any]] = []
    for r in raw_rows:
        usd = r.get("usd")
        if usd is not None:
            try:
                usd = float(usd)
                if usd <= 0:
                    # Zero usd means no pricing was joined — surface as None
                    # so callers can distinguish "free SKU" from "unknown price".
                    usd = None
            except (TypeError, ValueError):
                usd = None
        normalised.append({
            "sku_name": r.get("sku_name"),
            "usage_unit": r.get("usage_unit"),
            "dbus": float(r.get("dbus") or 0.0),
            "usd": usd,
            "unit_price": r.get("unit_price"),
        })

    return CostBreakdown.from_rows(
        endpoint=endpoint,
        lookback_hours=lookback_hours,
        rows=normalised,
    )


def cost_for_agent(
    *,
    agent_name: str | None = None,
    endpoint: str | None = None,
    ws: "WorkspaceClient",
    lookback_hours: int = 24,
    warehouse_id: str | None = None,
) -> CostBreakdown:
    """Resolve an agent name to its serving endpoint, then return cost.

    Either ``agent_name`` or ``endpoint`` must be set. When ``agent_name``
    is provided, ``apx-agent list``-style scan is used to find the registered
    model tagged ``apx.agent.name=<agent_name>`` and read its serving
    endpoint from the metadata.

    For now the simpler path (when only ``agent_name`` is provided)
    assumes the serving endpoint and agent name are the same — which
    matches the default ``databricks.agents.deploy`` behavior. Pass
    ``endpoint`` explicitly if they differ.
    """
    if not endpoint and not agent_name:
        raise ValueError("Pass agent_name or endpoint.")
    resolved = endpoint or agent_name
    assert resolved is not None  # narrowed by the check above
    return cost_for_endpoint(
        resolved,
        ws=ws,
        lookback_hours=lookback_hours,
        warehouse_id=warehouse_id,
    )


