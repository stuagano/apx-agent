"""Customer triage agent — a worked example exercising the full apx-agent surface.

Demonstrates:
  * ``@tool(uc=...)`` decorator for pure-Python tools that should live in UC
  * ``Dependencies.Workspace`` for tools that need user-scoped OBO auth
  * ``genie_tool`` for natural-language data questions
  * ``vector_search_tool`` for retrieval over a doc index
  * ``HandoffAgent`` for routing to specialists mid-conversation
  * Declared resources flowing through to ``log_agent``
  * Sessions when paired with ``compile_to_chat_agent(..., session_store=...)``

This file is the agent definition. ``app.py`` wraps it as a FastAPI app for
local dev. ``evalset.jsonl`` exercises the routing decisions.

Run locally::

    cd python/examples/customer_triage
    uv pip install -e ../..
    apx run

Publish + deploy::

    apx publish-tools --module agent:triage_agent
    apx deploy --module agent:triage_agent \
               --model databricks-claude-sonnet-4-6 \
               --name main.agents.customer_triage
"""

from __future__ import annotations

from apx_agent import (
    Agent,
    Dependencies,
    HandoffAgent,
    genie_tool,
    tool,
    vector_search_tool,
)


# ---------------------------------------------------------------------------
# Pure-Python tools — live in UC, callable from anything UC-aware
# ---------------------------------------------------------------------------


@tool(uc="main.agent_tools.classify_intent", grant=["agent_consumers"])
def classify_intent(query: str) -> str:
    """Classify a customer query as billing, technical, account, or other.

    Returns one of: "billing", "technical", "account", "other".
    """
    q = query.lower()
    if any(word in q for word in ("bill", "invoice", "charge", "payment", "refund")):
        return "billing"
    if any(word in q for word in ("error", "bug", "broken", "crash", "not working")):
        return "technical"
    if any(word in q for word in ("password", "login", "account", "email", "username")):
        return "account"
    return "other"


@tool(uc="main.agent_tools.format_address", grant=["agent_consumers"])
def format_address(street: str, city: str, region: str, postal_code: str) -> str:
    """Format a postal address into a canonical multi-line string."""
    return f"{street}\n{city}, {region} {postal_code}"


# ---------------------------------------------------------------------------
# Tools that need user-scoped Databricks auth — Python-only, not UC-syncable
# ---------------------------------------------------------------------------


@tool
def get_recent_orders(
    customer_id: str,
    ws: Dependencies.Workspace,
) -> list[dict]:
    """Return the customer's last 5 orders from the orders table.

    Runs as the calling user; UC grants on main.sales.orders apply.
    """
    from apx_agent import run_sql

    rows = run_sql(
        ws,
        "SELECT order_id, total, status, ordered_at FROM main.sales.orders "
        "WHERE customer_id = :cid ORDER BY ordered_at DESC LIMIT 5",
        parameters=[{"name": "cid", "value": customer_id, "type": "STRING"}],
    )
    return rows


# ---------------------------------------------------------------------------
# Specialist agents — each handles one branch of triage
# ---------------------------------------------------------------------------


billing_agent = Agent(
    name="billing_specialist",
    instructions=(
        "You're a billing specialist. Answer questions about invoices, charges, "
        "refunds, and payment methods. Use get_recent_orders to look up the "
        "customer's order history when relevant."
    ),
    tools=[get_recent_orders, format_address],
)


technical_agent = Agent(
    name="technical_specialist",
    instructions=(
        "You're a technical specialist. Answer questions about product errors, "
        "outages, and integration issues. Use the docs_search tool to find "
        "relevant troubleshooting articles before answering."
    ),
    tools=[
        vector_search_tool(
            "main.support.docs_index",
            columns=["doc_id", "title", "content", "url"],
            num_results=5,
            name="docs_search",
            description="Search the support documentation index.",
        ),
    ],
)


account_agent = Agent(
    name="account_specialist",
    instructions=(
        "You're an account specialist. Help with password resets, email changes, "
        "and account access. Use ask_account_data to look up the customer's "
        "account record when relevant."
    ),
    tools=[
        genie_tool(
            "$ACCOUNT_GENIE_SPACE_ID",  # resolved from env at startup
            name="ask_account_data",
            description="Ask a natural-language question about a customer's account.",
        ),
    ],
)


# ---------------------------------------------------------------------------
# Top-level triage agent — routes via HandoffAgent
# ---------------------------------------------------------------------------


triage_classifier = Agent(
    name="triage",
    instructions=(
        "You're a customer support triage agent. First, call classify_intent on "
        "the user's question. Then call exactly one of the transfer tools based "
        "on the result:\n"
        "  billing   -> transfer_to_billing_specialist\n"
        "  technical -> transfer_to_technical_specialist\n"
        "  account   -> transfer_to_account_specialist\n"
        "If intent is 'other', answer directly with a polite acknowledgment and "
        "do not transfer."
    ),
    tools=[classify_intent],
)


# The variable name 'agent' is what `apx run` / `apx info` look for by default,
# so the HandoffAgent gets that name.
agent = HandoffAgent(
    agents={
        "triage": triage_classifier,
        "billing_specialist": billing_agent,
        "technical_specialist": technical_agent,
        "account_specialist": account_agent,
    },
    start="triage",
)
