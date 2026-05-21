"""Pure-logic helpers registered as UC functions.

These are deterministic transformations on data the agent has already
fetched user-side. Hosting them as UC functions means Genie, Managed
MCP, and other agents can reuse the same classification without
re-implementing the thresholds — and the data team owns the rules in
one place (this file → UC after ``apx publish-tools``).

Today's set:

  * ``classify_shortage_severity`` — combines historical price-delta
    pct + current customer-count signal into a severity label.

What's NOT here (and why): anything that touches a WorkspaceClient or
external API. UC functions execute server-side under the function
owner's identity, so they can't honor the calling user's grants on
Genie / Vector Search / SQL warehouses. The data-fetching tools in
``agent_router.py`` stay Python-only by design.
"""

from __future__ import annotations

from apx_agent import tool


@tool(
    uc="main.shortage_tools.classify_shortage_severity",
    grant=["data_analysts", "agent_consumers"],
)
def classify_shortage_severity(
    avg_price_delta_pct: float,
    max_price_delta_pct: float,
    customer_count: int,
    similar_events_found: int,
) -> str:
    """Classify a shortage signal's severity from demand + historical pattern signals.

    Returns one of ``"CRITICAL"``, ``"HIGH"``, ``"MEDIUM"``, ``"LOW"``,
    ``"NOVEL"``. The thresholds encode the sourcing team's standing
    playbook — change them here, re-publish to UC, and Genie picks up
    the new rule on its next call.

    Rules:

      * ``CRITICAL`` — peer events show >40% peak price spike AND >=4
        customers in the current cluster.
      * ``HIGH`` — peer events average >25% spike OR >=4 customers.
      * ``MEDIUM`` — peer events average >10% spike OR >=2 customers.
      * ``LOW`` — peer events exist and didn't spike much.
      * ``NOVEL`` — no historical events; current signal is unbaselined.
    """
    if similar_events_found == 0:
        return "NOVEL"
    if max_price_delta_pct > 40.0 and customer_count >= 4:
        return "CRITICAL"
    if avg_price_delta_pct > 25.0 or customer_count >= 4:
        return "HIGH"
    if avg_price_delta_pct > 10.0 or customer_count >= 2:
        return "MEDIUM"
    return "LOW"
