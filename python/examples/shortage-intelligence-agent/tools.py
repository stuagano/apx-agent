"""Shortage intelligence tools.

Each tool is a plain typed Python function. apx-agent converts the signatures
to JSON schema and registers them as LLM-callable tools automatically.

Tools are grouped by pipeline step and imported by pipeline.py.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from apx_agent import Dependencies, ResourceSpec, attach_resources, decode_statement, run_sql
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.dashboards import GenieMessage, MessageStatus

from config import get_settings
from models import (
    AlternativePart,
    HistoricalPattern,
    ShortageSignal,
    VendorListing,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 1: Demand Cluster Detection
# ---------------------------------------------------------------------------

def scan_demand_clusters(
    lookback_hours: int,
    min_customer_count: int,
    ws: Dependencies.Workspace,
) -> dict[str, Any]:
    """Scan internal demand orders for components requested by multiple customers
    within the lookback window. Returns structured shortage signals ranked by
    customer count. Use lookback_hours=48 and min_customer_count=2 for the
    standard daily scan."""
    settings = get_settings()

    if settings.demand_orders_table:
        rows = run_sql(
            ws,
            f"""
            SELECT
                component_id,
                component_name,
                COUNT(DISTINCT customer_id) AS customer_count,
                MIN(requested_at)           AS earliest_request,
                MAX(requested_at)           AS latest_request,
                SUM(quantity_requested)     AS total_units_requested
            FROM {settings.demand_orders_table}
            WHERE requested_at >= DATEADD(HOUR, -{lookback_hours}, CURRENT_TIMESTAMP())
            GROUP BY component_id, component_name
            HAVING COUNT(DISTINCT customer_id) >= {min_customer_count}
            ORDER BY customer_count DESC
            LIMIT 15
            """,
            warehouse_id=settings.databricks_warehouse_id or None,
        )
        signals = [
            ShortageSignal(
                component_id=str(r["component_id"]),
                component_name=str(r["component_name"]),
                customer_count=int(r["customer_count"]),
                earliest_request=str(r["earliest_request"]),
                latest_request=str(r["latest_request"]),
                total_units_requested=int(r["total_units_requested"]),
                confidence="HIGH" if int(r["customer_count"]) >= 4 else "MEDIUM",
            )
            for r in rows
        ]
    elif settings.demand_genie_space_id:
        signals = _scan_via_genie(lookback_hours, min_customer_count, ws)
    else:
        return {
            "error": "Neither DEMAND_ORDERS_TABLE nor DEMAND_GENIE_SPACE_ID is configured."
        }

    return {
        "scan_window_hours": lookback_hours,
        "min_customer_threshold": min_customer_count,
        "signal_count": len(signals),
        "signals": [s.model_dump() for s in signals],
    }


def _genie_rows(ws: WorkspaceClient, space_id: str, msg: GenieMessage) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for att in msg.attachments or []:
        if att.query is None or not att.attachment_id:
            continue
        try:
            qr = ws.genie.get_message_query_result_by_attachment(
                space_id=space_id,
                conversation_id=msg.conversation_id,
                message_id=msg.message_id,
                attachment_id=att.attachment_id,
            )
            rows.extend(decode_statement(qr.statement_response))
        except Exception as e:
            logger.warning("Failed to fetch Genie query result: %s", e)
    return rows


def _scan_via_genie(
    lookback_hours: int,
    min_customer_count: int,
    ws: WorkspaceClient,
) -> list[ShortageSignal]:
    settings = get_settings()
    space_id = settings.demand_genie_space_id

    question = (
        f"List all components requested by at least {min_customer_count} distinct customers "
        f"in the last {lookback_hours} hours. Include: component_id, component_name, "
        f"number of distinct customers, earliest request time, latest request time, "
        f"and total units requested. Return as a table."
    )

    msg = ws.genie.start_conversation_and_wait(space_id=space_id, content=question)
    if msg.status != MessageStatus.COMPLETED:
        logger.warning("Genie demand scan ended with status %s: %s", msg.status, msg.error)
        return []

    signals: list[ShortageSignal] = []
    for r in _genie_rows(ws, space_id, msg):
        count = int(r.get("customer_count", r.get("distinct_customers", 0)) or 0)
        signals.append(ShortageSignal(
            component_id=str(r.get("component_id", "")),
            component_name=str(r.get("component_name", "")),
            customer_count=count,
            earliest_request=str(r.get("earliest_request", "")),
            latest_request=str(r.get("latest_request", "")),
            total_units_requested=int(r.get("total_units_requested", r.get("units_requested", 0)) or 0),
            confidence="HIGH" if count >= 4 else "MEDIUM",
        ))
    return signals


# ---------------------------------------------------------------------------
# Step 2: Historical Pattern Lookup
# ---------------------------------------------------------------------------

def find_historical_patterns(
    component_id: str,
    lookback_years: int,
    ws: Dependencies.Workspace,
) -> dict[str, Any]:
    """Look up historical shortage events for this component. Returns prior
    shortage occurrences, average and max price delta percentages, and average
    duration. Use to assess severity and set pricing expectations."""
    settings = get_settings()

    if not settings.historical_demand_table:
        return {
            "component_id": component_id,
            "data_available": False,
            "note": "HISTORICAL_DEMAND_TABLE not configured.",
        }

    rows = run_sql(
        ws,
        f"""
        SELECT
            event_date,
            price_before_usd,
            price_peak_usd,
            ROUND((price_peak_usd - price_before_usd) / price_before_usd * 100, 1) AS price_delta_pct,
            shortage_duration_days,
            resolution_notes
        FROM {settings.historical_demand_table}
        WHERE component_id = :component_id
          AND event_date >= DATEADD(YEAR, -{lookback_years}, CURRENT_DATE())
        ORDER BY event_date DESC
        LIMIT 10
        """,
        warehouse_id=settings.databricks_warehouse_id or None,
        parameters=[{"name": "component_id", "value": component_id, "type": "STRING"}],
    )

    if not rows:
        return HistoricalPattern(
            component_id=component_id,
            similar_events_found=0,
            avg_price_delta_pct=0.0,
            max_price_delta_pct=0.0,
            avg_shortage_duration_days=0,
            recent_events=[],
        ).model_dump()

    deltas = [float(r.get("price_delta_pct", 0)) for r in rows]
    durations = [int(r.get("shortage_duration_days", 0)) for r in rows]

    return HistoricalPattern(
        component_id=component_id,
        similar_events_found=len(rows),
        avg_price_delta_pct=round(sum(deltas) / len(deltas), 1),
        max_price_delta_pct=round(max(deltas), 1),
        avg_shortage_duration_days=round(sum(durations) / len(durations)),
        recent_events=rows[:5],
    ).model_dump()


# ---------------------------------------------------------------------------
# Step 3: Market Signal Validation — Vector Search (agent synthesizes verdict)
# ---------------------------------------------------------------------------

def validate_against_market_news(
    component_id: str,
    manufacturer: str,
    ws: Dependencies.Workspace,
) -> dict[str, Any]:
    """Search market intelligence documents for evidence about a shortage signal.

    Returns sources and snippets only — the calling agent synthesizes
    CONFIRMED / UNCONFIRMED from this evidence. Does not make a nested LLM
    call (keeps tracing and max_iterations on the outer agent loop).

    Use after demand cluster detection to separate noise from real market events.
    """
    settings = get_settings()

    if not (settings.vs_endpoint and settings.vs_index):
        return {
            "component_id": component_id,
            "configured": False,
            "sources": [],
            "snippets": [],
            "note": "Market validation not configured — set VS_ENDPOINT and VS_INDEX.",
        }

    try:
        query_text = f"{manufacturer} {component_id} shortage supply constraint pricing"
        result = ws.vector_search_indexes.query_index(
            index_name=settings.vs_index,
            columns=["chunk_id", "source_file", "content"],
            query_text=query_text,
            num_results=5,
        )

        docs: list[str] = []
        sources: list[str] = []
        if result.result and result.result.data_array:
            col_names = (
                [c.name for c in result.manifest.columns]
                if result.manifest
                else ["chunk_id", "source_file", "content", "score"]
            )
            for row in result.result.data_array:
                r = dict(zip(col_names, row))
                content = r.get("content", "")
                if content:
                    docs.append(content[:800])
                src = r.get("source_file", "unknown")
                if src not in sources:
                    sources.append(src)

        if not docs:
            return {
                "component_id": component_id,
                "configured": True,
                "sources": [],
                "snippets": [],
                "note": (
                    f"No market intelligence found for {manufacturer} {component_id}. "
                    "Treat as UNCONFIRMED unless other evidence exists."
                ),
            }

        return {
            "component_id": component_id,
            "configured": True,
            "sources": sources[:5],
            "snippets": docs[:5],
            "note": (
                "Synthesize CONFIRMED or UNCONFIRMED from these snippets; "
                "cite sources in your report."
            ),
        }

    except Exception as e:
        logger.warning("Vector Search validation failed for %s: %s", component_id, e)
        return {
            "component_id": component_id,
            "configured": True,
            "sources": [],
            "snippets": [],
            "error": str(e),
            "note": "Market validation error — treat as UNCONFIRMED.",
        }


# ---------------------------------------------------------------------------
# Step 4a: Live Vendor Pricing (DigiKey)
# ---------------------------------------------------------------------------

def check_vendor_availability(part_numbers: list[str]) -> dict[str, Any]:
    """Query DigiKey for live pricing and stock availability for a list of
    part numbers. Returns unit price, quantity in stock, and lead time for each
    part. Use to identify where to buy and at what price before the shortage
    drives prices up."""
    settings = get_settings()

    if not settings.digikey_client_id or not settings.digikey_client_secret:
        return {
            "vendor": "DigiKey",
            "configured": False,
            "note": "DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET not configured.",
            "stub_results": [
                VendorListing(
                    part_number=pn,
                    manufacturer="(configure DigiKey API)",
                    vendor="DigiKey",
                    unit_price_usd=0.0,
                    quantity_available=0,
                    lead_time_weeks=None,
                    product_url=None,
                ).model_dump()
                for pn in part_numbers
            ],
        }

    token = _get_digikey_token(settings.digikey_client_id, settings.digikey_client_secret)
    listings: list[dict[str, Any]] = []

    for part_number in part_numbers:
        try:
            resp = httpx.get(
                f"https://api.digikey.com/products/v4/search/{part_number}/productdetails",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-DIGIKEY-Client-Id": settings.digikey_client_id,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            product = data.get("Product", {})
            pricing = product.get("UnitPrice", 0.0)
            qty = product.get("QuantityAvailable", 0)
            lead = product.get("LeadWeeks", None)

            listings.append(VendorListing(
                part_number=part_number,
                manufacturer=product.get("Manufacturer", {}).get("Name", ""),
                vendor="DigiKey",
                unit_price_usd=float(pricing),
                quantity_available=int(qty),
                lead_time_weeks=int(lead) if lead else None,
                product_url=product.get("ProductUrl"),
            ).model_dump())
        except Exception as e:
            logger.warning("DigiKey lookup failed for %s: %s", part_number, e)
            listings.append({"part_number": part_number, "error": str(e)})

    return {"vendor": "DigiKey", "listings": listings}


def _get_digikey_token(client_id: str, client_secret: str) -> str:
    resp = httpx.post(
        "https://api.digikey.com/v1/oauth2/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Step 4b: Alternative Part Lookup
# ---------------------------------------------------------------------------

def find_alternative_parts(
    component_id: str,
    max_results: int,
    ws: Dependencies.Workspace,
) -> dict[str, Any]:
    """Search the parts catalog for alternative components that meet the same
    electrical specifications as the flagged part but come from different
    manufacturers. Use to give sourcing team backup options if the primary
    component is unavailable or priced too high."""
    settings = get_settings()

    if not settings.parts_catalog_table:
        return {
            "component_id": component_id,
            "configured": False,
            "note": "PARTS_CATALOG_TABLE not configured.",
        }

    rows = run_sql(
        ws,
        f"""
        SELECT
            alt.part_number          AS alt_part_number,
            alt.manufacturer         AS alt_manufacturer,
            alt.package_type,
            alt.voltage_rating_v,
            alt.current_rating_a,
            alt.temperature_range,
            alt.in_stock,
            ROUND(
                1.0 - (
                    ABS(alt.voltage_rating_v - ref.voltage_rating_v) / NULLIF(ref.voltage_rating_v, 0) +
                    ABS(alt.current_rating_a - ref.current_rating_a) / NULLIF(ref.current_rating_a, 0)
                ) / 2.0,
                2
            ) AS spec_match_score
        FROM {settings.parts_catalog_table} ref
        JOIN {settings.parts_catalog_table} alt
            ON  alt.package_type   = ref.package_type
            AND alt.part_number   != ref.part_number
            AND alt.manufacturer  != ref.manufacturer
        WHERE ref.part_number = :component_id
          AND alt.in_stock = true
        ORDER BY spec_match_score DESC
        LIMIT {max_results}
        """,
        warehouse_id=settings.databricks_warehouse_id or None,
        parameters=[{"name": "component_id", "value": component_id, "type": "STRING"}],
    )

    alternatives = [
        AlternativePart(
            original_part_number=component_id,
            alt_part_number=str(r["alt_part_number"]),
            alt_manufacturer=str(r["alt_manufacturer"]),
            spec_match_score=float(r.get("spec_match_score", 0.0) or 0.0),
            key_differences=f"pkg={r.get('package_type')}, {r.get('voltage_rating_v')}V, {r.get('current_rating_a')}A",
            in_stock=bool(r.get("in_stock", False)),
        ).model_dump()
        for r in rows
    ]

    return {
        "component_id": component_id,
        "alternatives_found": len(alternatives),
        "alternatives": alternatives,
    }


# ---------------------------------------------------------------------------
# Resource declarations for log_agent / Model Serving manifests
# ---------------------------------------------------------------------------

_settings = get_settings()
if _settings.demand_orders_table:
    attach_resources(
        scan_demand_clusters,
        [ResourceSpec("uc_table", _settings.demand_orders_table)],
    )
if _settings.demand_genie_space_id:
    attach_resources(
        scan_demand_clusters,
        [ResourceSpec("genie_space", _settings.demand_genie_space_id)],
    )
if _settings.historical_demand_table:
    attach_resources(
        find_historical_patterns,
        [ResourceSpec("uc_table", _settings.historical_demand_table)],
    )
if _settings.vs_index:
    attach_resources(
        validate_against_market_news,
        [ResourceSpec("vector_search_index", _settings.vs_index)],
    )
if _settings.parts_catalog_table:
    attach_resources(
        find_alternative_parts,
        [ResourceSpec("uc_table", _settings.parts_catalog_table)],
    )
if _settings.databricks_warehouse_id:
    _wh = [ResourceSpec("sql_warehouse", _settings.databricks_warehouse_id)]
    for _fn in (
        scan_demand_clusters,
        find_historical_patterns,
        find_alternative_parts,
    ):
        attach_resources(_fn, _wh)

