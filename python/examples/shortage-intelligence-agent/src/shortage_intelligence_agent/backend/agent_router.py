"""Shortage intelligence tools.

Each tool is a plain typed Python function. apx-agent converts the signatures
to JSON schema and registers them as LLM-callable tools automatically.

Tools are grouped by pipeline step and imported by pipeline.py.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from apx_agent import Dependencies, decode_statement, run_sql
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.dashboards import GenieMessage, MessageStatus

from .config import get_settings
from .models import (
    AlternativePart,
    HistoricalPattern,
    MarketSignal,
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
# Step 3: Market Signal Validation — Vector Search + LLM synthesis
# ---------------------------------------------------------------------------

def validate_against_market_news(
    component_id: str,
    manufacturer: str,
    ws: Dependencies.Workspace,
) -> dict[str, Any]:
    """Search market intelligence documents for evidence confirming or
    contradicting a shortage signal for this component. Returns a structured
    verdict with confidence and supporting source references. Use after demand
    cluster detection to separate noise from real market events."""
    settings = get_settings()

    if not (settings.vs_endpoint and settings.vs_index):
        return MarketSignal(
            component_id=component_id,
            confirmed=False,
            confidence="LOW",
            supporting_sources=[],
            summary="Market validation not configured — set VS_ENDPOINT and VS_INDEX.",
        ).model_dump()

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
                docs.append(r.get("content", ""))
                src = r.get("source_file", "unknown")
                if src not in sources:
                    sources.append(src)

        if not docs:
            return MarketSignal(
                component_id=component_id,
                confirmed=False,
                confidence="LOW",
                supporting_sources=[],
                summary=f"No market intelligence found for {manufacturer} {component_id}.",
            ).model_dump()

        # Synthesize a verdict using Sonnet — faster + cheaper than Opus for yes/no verdicts
        from databricks_langchain import ChatDatabricks
        from langchain_core.messages import HumanMessage, SystemMessage

        context = "\n\n---\n\n".join(docs[:5])
        llm = ChatDatabricks(model="databricks-claude-sonnet-4-6")
        verdict = llm.invoke([
            SystemMessage(content=(
                "You are a semiconductor market analyst. Based on the market intelligence "
                "documents provided, determine whether there is evidence of a shortage or "
                "significant price movement for the specified component. "
                "Respond with: CONFIRMED or UNCONFIRMED, confidence (HIGH/MEDIUM/LOW), "
                "and a 2-3 sentence summary citing specific evidence from the documents."
            )),
            HumanMessage(content=(
                f"Component: {manufacturer} {component_id}\n\n"
                f"Market Intelligence Documents:\n{context}"
            )),
        ])

        answer = verdict.content
        answer_upper = answer.upper()
        confirmed = "CONFIRMED" in answer_upper and "UNCONFIRMED" not in answer_upper
        confidence = "HIGH" if "HIGH" in answer_upper else ("MEDIUM" if "MEDIUM" in answer_upper else "LOW")

        return MarketSignal(
            component_id=component_id,
            confirmed=confirmed,
            confidence=confidence,
            supporting_sources=sources[:5],
            summary=answer[:500],
        ).model_dump()

    except Exception as e:
        logger.warning("Vector Search validation failed for %s: %s", component_id, e)
        return MarketSignal(
            component_id=component_id,
            confirmed=False,
            confidence="LOW",
            supporting_sources=[],
            summary=f"Market validation error: {e}",
        ).model_dump()


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
# Ad-hoc Genie exploration tool
# ---------------------------------------------------------------------------

def query_genie(question: str, ws: Dependencies.Workspace) -> dict[str, Any]:
    """Ask a natural language question about demand orders, shortage history,
    or parts catalog data using Databricks Genie. Use this for ad-hoc
    exploration when the specific hardcoded queries don't cover what you
    need — for example, 'show me all orders from Dell for DDR5 parts in
    the last week' or 'what was the average price delta for NAND shortages
    in 2025'. Returns the Genie-generated SQL results."""
    settings = get_settings()

    if not settings.demand_genie_space_id:
        return {"error": "DEMAND_GENIE_SPACE_ID not configured — Genie exploration unavailable."}

    space_id = settings.demand_genie_space_id
    try:
        msg = ws.genie.start_conversation_and_wait(space_id=space_id, content=question)
        if msg.status != MessageStatus.COMPLETED:
            return {
                "error": f"Genie query ended with status {msg.status}",
                "details": str(msg.error) if msg.error else None,
                "question": question,
            }

        results = _genie_rows(ws, space_id, msg)
        text_content = next(
            (att.text.content for att in (msg.attachments or [])
             if att.text and att.text.content),
            "",
        )
        generated_sql = next(
            (att.query.query for att in (msg.attachments or [])
             if att.query and att.query.query),
            "",
        )

        return {
            "question": question,
            "sql_results": results[:20],
            "result_count": len(results),
            "genie_response": text_content[:500] if text_content else "",
            "generated_sql": generated_sql,
        }

    except Exception as e:
        logger.warning("Genie query failed: %s", e)
        return {"error": f"Genie query failed: {e}", "question": question}


# ---------------------------------------------------------------------------
# Compose into pipeline — deferred import to break circular dependency
# (pipeline.py imports tool functions from this module)
# ---------------------------------------------------------------------------

def _build_agent():
    from .pipeline import create_shortage_pipeline
    return create_shortage_pipeline()


agent = _build_agent()
