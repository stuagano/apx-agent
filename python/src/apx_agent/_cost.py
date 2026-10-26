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

# Published FMAPI list prices used by the traces panel and session usd caps (#836).
# Values are USD per million tokens (input, output).
_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "databricks-claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}


def usd_for_token_usage(input_tokens: int, output_tokens: int, *, model: str | None) -> float | None:
    """Dollar cost of one usage blob, or ``None`` when the model has no published rate."""
    if model is None or not str(model).strip():
        return None
    key = str(model).strip()
    if key.startswith("databricks:/"):
        key = key[len("databricks:/"):]
    rates = _USD_PER_MTOK.get(key)
    if rates is None and key.startswith("databricks-"):
        rates = _USD_PER_MTOK.get(key[len("databricks-"):])
    if rates is None:
        return None
    inp, out = rates
    return (input_tokens * inp + output_tokens * out) / 1_000_000


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Per-endpoint cost
# ---------------------------------------------------------------------------






