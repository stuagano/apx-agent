"""Shortage intelligence pipeline.

Five-step SequentialAgent — structurally guarantees each step runs in order.
Each step's output accumulates in the conversation history, so the synthesis
step sees all prior findings without any extra wiring.

Step order:
  1. Demand Scanner   — detect 48-hour clustering signals
  2. Historical Analyst — price delta and duration from past shortages
  3. Market Validator  — KA + news confirmation
  4. Vendor Pricer     — live DigiKey pricing + alternative parts
  5. Report Generator  — dual reports for sourcing and sales teams
"""

from __future__ import annotations

from apx_agent import Agent, SequentialAgent, genie_query_tool

from .config import get_settings


def create_shortage_pipeline() -> SequentialAgent:
    from .agent_router import (
        scan_demand_clusters,
        find_historical_patterns,
        validate_against_market_news,
        check_vendor_availability,
        find_alternative_parts,
    )
    from .uc_helpers import classify_shortage_severity

    settings = get_settings()
    # Ad-hoc Genie exploration is added as an optional tool when a space is
    # configured. The LLM chooses to call it when the canned queries don't
    # cover what the user actually asked.
    ad_hoc_explorer = (
        [
            genie_query_tool(
                settings.demand_genie_space_id,
                description=(
                    "Explore demand orders, shortage history, or parts catalog data "
                    "via natural-language SQL. Use when the canned queries don't fit "
                    "(e.g. cross-customer cohorts, ad-hoc time windows, vendor-specific filters)."
                ),
            )
        ]
        if settings.demand_genie_space_id
        else []
    )

    # ------------------------------------------------------------------
    # Step 1: Demand Cluster Detection
    # ------------------------------------------------------------------
    detection_agent = Agent(
        tools=[scan_demand_clusters],
        instructions=(
            "You are the Demand Cluster Detector.\n\n"
            "Your job: identify components showing multi-customer demand clustering "
            "in the last 48 hours — the early signal of a shortage forming.\n\n"
            "Call scan_demand_clusters with lookback_hours=48 and min_customer_count=2.\n\n"
            "Report:\n"
            "- How many signals were found\n"
            "- Each signal: component ID/name, customer count, request window, total units\n"
            "- Confidence level for each signal\n"
            "- Which signals to prioritize for deeper investigation (HIGH confidence first)\n\n"
            "If no signals are found, say so clearly and end the pipeline early."
        ),
    )

    # ------------------------------------------------------------------
    # Step 2: Historical Pattern Analysis
    # ------------------------------------------------------------------
    historical_agent = Agent(
        tools=[find_historical_patterns, classify_shortage_severity, *ad_hoc_explorer],
        instructions=(
            "You are the Historical Pattern Analyst.\n\n"
            "The demand scan above found shortage signals. For each HIGH or MEDIUM "
            "confidence signal, call find_historical_patterns with the component_id "
            "and lookback_years=5.\n\n"
            "Then for each component, call classify_shortage_severity with the "
            "historical avg_price_delta_pct, max_price_delta_pct, the current "
            "customer_count from the demand scan, and similar_events_found from "
            "the historical lookup. The function returns a severity label "
            "(CRITICAL / HIGH / MEDIUM / LOW / NOVEL) encoding the sourcing "
            "team's standing playbook.\n\n"
            "If find_historical_patterns returns no events for a component but the "
            "demand signal is strong, use query_genie to explore related patterns "
            "(by manufacturer, package type, or similar parts).\n\n"
            "Report for each component:\n"
            "- How many similar shortage events occurred historically\n"
            "- Average and max price delta percentage during past shortages\n"
            "- Average shortage duration in days\n"
            "- The classify_shortage_severity verdict\n"
            "- Whether this component has a pattern of recurring shortages\n\n"
            "Summarize overall severity: are these components historically volatile "
            "or is this a novel pattern?"
        ),
    )

    # ------------------------------------------------------------------
    # Step 3: Market Signal Validation
    # ------------------------------------------------------------------
    market_agent = Agent(
        tools=[validate_against_market_news],
        instructions=(
            "You are the Market Signal Validator.\n\n"
            "For each component with a HIGH or MEDIUM demand signal, call "
            "validate_against_market_news with the component_id and manufacturer.\n\n"
            "Report:\n"
            "- Whether market reports confirm or contradict the internal demand signal\n"
            "- Confidence level of the market validation\n"
            "- Key sources cited\n"
            "- Which signals are now CONFIRMED (demand + market both signal shortage) "
            "vs UNCONFIRMED (demand signal only)\n\n"
            "A CONFIRMED signal warrants immediate action. An UNCONFIRMED signal "
            "warrants monitoring."
        ),
    )

    # ------------------------------------------------------------------
    # Step 4: Vendor Pricing and Alternative Parts
    # ------------------------------------------------------------------
    vendor_agent = Agent(
        tools=[check_vendor_availability, find_alternative_parts],
        instructions=(
            "You are the Vendor Intelligence Analyst.\n\n"
            "For each CONFIRMED shortage signal from the market validation step:\n\n"
            "1. Call check_vendor_availability with the component part numbers. "
            "Report current price, quantity in stock, and lead time.\n\n"
            "2. Call find_alternative_parts with component_id and max_results=5. "
            "Report the top alternatives by spec match score.\n\n"
            "Structure your output as:\n"
            "- [Component ID]: $X.XX/unit, QTY available: N, Lead: N weeks\n"
            "- Top alternatives: [alt1 (score: 0.95), alt2 (score: 0.88), ...]\n\n"
            "Flag any component where quantity_available < total_units_requested "
            "from the demand scan — that is a critical supply gap."
        ),
    )

    # ------------------------------------------------------------------
    # Step 5: Dual Report Generation
    # ------------------------------------------------------------------
    report_agent = Agent(
        tools=[],
        instructions=(
            "You are the Report Synthesizer.\n\n"
            "All investigation steps are complete. The conversation above contains:\n"
            "1. Demand cluster signals (component IDs, customer counts, volumes)\n"
            "2. Historical pricing patterns (avg delta, duration)\n"
            "3. Market validation verdicts (CONFIRMED / UNCONFIRMED)\n"
            "4. Live vendor pricing and alternative parts\n\n"
            "Produce TWO clearly separated reports:\n\n"
            "---\n"
            "## SOURCING TEAM REPORT\n\n"
            "**Priority**: URGENT / WATCH / MONITOR\n\n"
            "**Shortage Signals** (confirmed only):\n"
            "- [Component]: N customers, [earliest]-[latest], [units] units requested\n\n"
            "**Buy Now** (before prices spike):\n"
            "- [Component] via DigiKey: $X.XX/unit, N in stock, N weeks lead\n"
            "- ...\n\n"
            "**Alternative Parts** (if primary is scarce):\n"
            "- [Component] → [Alt part] ([Manufacturer]), spec match [score]\n"
            "- ...\n\n"
            "**Action Items**:\n"
            "- [ ] Call DigiKey rep for [component] — request volume hold\n"
            "- [ ] Place PO for [qty] units of [component] by EOD\n"
            "- ...\n\n"
            "---\n"
            "## SALES TEAM REPORT\n\n"
            "**Escalation**: IMMEDIATE / 48HR / MONITOR\n\n"
            "**Customers to Alert Proactively**:\n"
            "List customers who placed orders for CONFIRMED shortage components.\n"
            "For each: customer name, component ordered, quantity, order date.\n\n"
            "**Price Forecast**: Based on historical patterns, [component] typically "
            "spikes [X%] over [Y] days. Current window: [date range].\n\n"
            "**Proactive Alert Template**:\n"
            "---\n"
            "Subject: Important Update on Your Order for [Component]\n\n"
            "Hi [Name],\n\n"
            "We are reaching out proactively about [component] in your recent order. "
            "Industry demand signals suggest availability may tighten in the next "
            "[timeframe]. We recommend [action]. Please contact your account manager "
            "at [contact] to discuss options.\n"
            "---\n\n"
            "Use specific numbers and dates from the investigation. "
            "Do not generalize — cite exact component IDs, prices, and customer names."
        ),
    )

    return SequentialAgent(
        agents=[
            detection_agent,
            historical_agent,
            market_agent,
            vendor_agent,
            report_agent,
        ],
        instructions=(
            "You are the Shortage Intelligence Agent for a parts distributor. "
            "You run a five-step investigation to detect shortage signals, validate "
            "them against historical data and market reports, check live vendor "
            "availability, and produce actionable reports for the sourcing and sales teams. "
            "Follow each step in order — each step builds on the prior findings."
        ),
    )
