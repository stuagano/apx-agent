"""apx-agent DSL version of the shortage_intel pipeline.

Mirror of the customer's hand-written ``shortage-intelligence-agent-main``
LangGraph pipeline, expressed declaratively. This file is the **A/B
counterpart** to the reference implementation at::

    /Users/stuart.gano/Documents/Ahkbar/shortage-intelligence-agent-main/src/shortage_intel/

The reference repo distributes the logic across ``app.py`` (FastAPI wiring,
OBO header handling, SSE streaming, MCP mount), ``auth.py`` (workspace client
resolution), ``graph.py`` (LangGraph StateGraph wiring + temperature-strip
subclass), ``tools.py`` (closure-bound @tool definitions), and ``prompts.py``.
This DSL version expresses the same pipeline with:

  * No FastAPI boilerplate (apx-agent's ``create_app`` mounts everything).
  * No auth helper (``Dependencies.Workspace`` resolves OBO per request).
  * No StateGraph wiring (``SequentialAgent`` compiles to one).
  * No temperature subclass (encoded in ``_compile._build_chat_databricks``).
  * No ``make_tools(ws)`` factory (closure is implicit in the dep system).

The compiled output is functionally equivalent: same 5 nodes, same names,
same linear wiring, same per-request user-scoped ws closure pattern.

Run::

    from apx_agent import chat_agent_for, compile_to_langgraph
    chat_agent = chat_agent_for(pipeline, model="databricks-claude-sonnet-4-6")

    # Or, if hosting on Databricks Apps directly (skipping the ChatAgent layer):
    graph = compile_to_langgraph(pipeline, ws=user_ws, model="...")
    result = graph.invoke({"messages": [HumanMessage(content=user_prompt)]})
"""

from __future__ import annotations

import json
import os

from apx_agent import Dependencies, LlmAgent, SequentialAgent


# ---------------------------------------------------------------------------
# Prompts (verbatim from reference repo's prompts.py, abbreviated for the demo)
# ---------------------------------------------------------------------------

STEP1_DEMAND_SCANNER = """You are the demand-scanner agent. Call scan_demand_clusters
with lookback_hours=48 and min_customer_count=2. Report the top components by
customer count."""

STEP2_HISTORICAL_ANALYST = """You are the historical-analyst agent. For each
component the scanner flagged, call find_historical_patterns with
lookback_years=5. Summarize prior shortages and typical price deltas."""

STEP3_MARKET_VALIDATOR = """You are the market-validator agent. For each flagged
component, call validate_against_market_news with the component_id and
manufacturer. Separate noise from real events."""

STEP4_VENDOR_PRICER = """You are the vendor-pricer agent. Call
check_vendor_availability for the flagged parts and find_alternative_parts
with max_results=5. Report current vendor prices and backup options."""

STEP5_REPORT_GENERATOR = """You are the report-generator agent. Synthesize the
investigation into two reports: one for the sourcing team (purchasing focus)
and one for sales (customer-impact focus)."""


# ---------------------------------------------------------------------------
# Tools — apx-agent style: typed Python functions with Dependencies.Workspace
#
# Notice what is NOT here vs. the reference repo's ``tools.py``:
#   * No ``make_tools(ws)`` closure factory — ``Dependencies.Workspace``
#     resolves OBO per request.
#   * No ``@tool`` decorator — apx-agent introspects type hints.
#   * No ``get_settings()`` boilerplate per tool — config via env.
#
# Tool implementations are kept thin for the demo; in production they would
# carry the same SQL/HTTP body as the reference repo.
# ---------------------------------------------------------------------------


def scan_demand_clusters(
    lookback_hours: int,
    min_customer_count: int,
    ws: Dependencies.Workspace,
) -> str:
    """Scan internal demand orders for components requested by multiple customers
    in the lookback window. Returns ranked shortage signals."""
    from apx_agent import run_sql

    table = os.environ["DEMAND_ORDERS_TABLE"]
    rows = run_sql(
        ws,
        f"""
        SELECT component_id, component_name,
               COUNT(DISTINCT customer_id) AS customer_count,
               MIN(requested_at) AS earliest_request,
               MAX(requested_at) AS latest_request,
               SUM(quantity_requested) AS total_units_requested
        FROM {table}
        WHERE requested_at >= DATEADD(HOUR, -{lookback_hours}, CURRENT_TIMESTAMP())
        GROUP BY component_id, component_name
        HAVING COUNT(DISTINCT customer_id) >= {min_customer_count}
        ORDER BY customer_count DESC
        LIMIT 15
        """,
    )
    return json.dumps({"signal_count": len(rows), "signals": rows})


def find_historical_patterns(
    component_id: str,
    lookback_years: int,
    ws: Dependencies.Workspace,
) -> str:
    """Look up historical shortage events for this component. Returns prior
    occurrences, average/max price delta percentages, and duration."""
    from apx_agent import run_sql

    table = os.environ["HISTORICAL_DEMAND_TABLE"]
    rows = run_sql(
        ws,
        f"""
        SELECT event_date, price_before_usd, price_peak_usd,
               ROUND((price_peak_usd - price_before_usd) / price_before_usd * 100, 1)
                 AS price_delta_pct,
               shortage_duration_days
        FROM {table}
        WHERE component_id = '{component_id}'
          AND event_date >= DATEADD(YEAR, -{lookback_years}, CURRENT_DATE())
        ORDER BY event_date DESC
        LIMIT 10
        """,
    )
    deltas = [float(r.get("price_delta_pct", 0)) for r in rows]
    return json.dumps({
        "component_id": component_id,
        "similar_events_found": len(rows),
        "avg_price_delta_pct": round(sum(deltas) / max(len(deltas), 1), 1),
        "max_price_delta_pct": round(max(deltas, default=0.0), 1),
    })


def validate_against_market_news(
    component_id: str,
    manufacturer: str,
    ws: Dependencies.Workspace,
) -> str:
    """Search market intelligence (Vector Search index) for evidence.
    Returns sources/snippets for the calling agent to synthesize CONFIRMED /
    UNCONFIRMED — no nested LLM call inside the tool."""
    index_name = os.environ["VS_INDEX"]
    result = ws.vector_search_indexes.query_index(
        index_name=index_name,
        columns=["chunk_id", "source_file", "content"],
        query_text=f"{manufacturer} {component_id} shortage supply constraint pricing",
        num_results=5,
    )
    sources, docs = [], []
    if result.result and result.result.data_array:
        col_names = [c.name for c in result.manifest.columns]
        for row in result.result.data_array:
            r = dict(zip(col_names, row))
            docs.append(r.get("content", ""))
            sources.append(r.get("source_file", "unknown"))
    return json.dumps({
        "component_id": component_id,
        "confirmed": bool(docs),
        "confidence": "HIGH" if len(docs) >= 3 else "MEDIUM" if docs else "LOW",
        "supporting_sources": sources[:5],
    })


def check_vendor_availability(part_numbers: list[str]) -> str:
    """Query DigiKey for live pricing and stock for a list of part numbers.
    Returns unit price, qty available, and lead time per part."""
    import httpx

    client_id = os.environ.get("DIGIKEY_CLIENT_ID")
    client_secret = os.environ.get("DIGIKEY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return json.dumps({"vendor": "DigiKey", "configured": False})

    token_resp = httpx.post(
        "https://api.digikey.com/v1/oauth2/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    token_resp.raise_for_status()
    token = token_resp.json()["access_token"]

    listings = []
    for pn in part_numbers:
        try:
            r = httpx.get(
                f"https://api.digikey.com/products/v4/search/{pn}/productdetails",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-DIGIKEY-Client-Id": client_id,
                },
                timeout=15,
            )
            r.raise_for_status()
            p = r.json().get("Product", {})
            listings.append({
                "part_number": pn,
                "manufacturer": p.get("Manufacturer", {}).get("Name", ""),
                "unit_price_usd": float(p.get("UnitPrice", 0.0)),
                "quantity_available": int(p.get("QuantityAvailable", 0)),
            })
        except Exception as e:
            listings.append({"part_number": pn, "error": str(e)})
    return json.dumps({"vendor": "DigiKey", "listings": listings})


def find_alternative_parts(
    component_id: str,
    max_results: int,
    ws: Dependencies.Workspace,
) -> str:
    """Search the parts catalog for alternative components with matching specs
    but different manufacturers."""
    from apx_agent import run_sql

    table = os.environ["PARTS_CATALOG_TABLE"]
    rows = run_sql(
        ws,
        f"""
        SELECT alt.part_number AS alt_part_number,
               alt.manufacturer AS alt_manufacturer,
               alt.in_stock
        FROM {table} ref
        JOIN {table} alt
          ON  alt.package_type = ref.package_type
          AND alt.part_number != ref.part_number
          AND alt.manufacturer != ref.manufacturer
        WHERE ref.part_number = '{component_id}'
          AND alt.in_stock = true
        LIMIT {max_results}
        """,
    )
    return json.dumps({
        "component_id": component_id,
        "alternatives_found": len(rows),
        "alternatives": rows,
    })


# ---------------------------------------------------------------------------
# The whole pipeline, expressed declaratively
# ---------------------------------------------------------------------------


pipeline = SequentialAgent(
    name="shortage_intelligence",
    agents=[
        LlmAgent(
            name="demand_scanner",
            tools=[scan_demand_clusters],
            instructions=STEP1_DEMAND_SCANNER,
        ),
        LlmAgent(
            name="historical_analyst",
            tools=[find_historical_patterns],
            instructions=STEP2_HISTORICAL_ANALYST,
        ),
        LlmAgent(
            name="market_validator",
            tools=[validate_against_market_news],
            instructions=STEP3_MARKET_VALIDATOR,
        ),
        LlmAgent(
            name="vendor_pricer",
            tools=[check_vendor_availability, find_alternative_parts],
            instructions=STEP4_VENDOR_PRICER,
        ),
        LlmAgent(
            name="report_generator",
            tools=[],
            instructions=STEP5_REPORT_GENERATOR,
        ),
    ],
)


# ---------------------------------------------------------------------------
# Wiring it up — pick ONE of these depending on hosting model
# ---------------------------------------------------------------------------

# (A) MLflow ChatAgent — loggable to UC, deployable to Model Serving, recognized
#     by AI Playground / Review App / Agent Eval.
def build_chat_agent():
    from apx_agent import chat_agent_for

    return chat_agent_for(pipeline, model="databricks-claude-sonnet-4-6")


# (B) Direct compile — simplest path for Databricks Apps hosting where you
#     build a per-request WorkspaceClient in your FastAPI route from the
#     X-Forwarded-Access-Token header.
def build_graph(user_ws):
    from apx_agent import compile_to_langgraph

    return compile_to_langgraph(
        pipeline, ws=user_ws, model="databricks-claude-sonnet-4-6"
    )
