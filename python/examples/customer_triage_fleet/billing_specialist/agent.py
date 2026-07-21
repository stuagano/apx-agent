"""billing_specialist: Handles billing inquiries, invoice lookups, and payment issues.

Standalone apx-agent app — serves its own A2A card, callable by any orchestrator
via ``sub_agents=[$BILLING_SPECIALIST_URL]``. APX_SMOKE_MODE=1 swaps
workspace-dependent tools for in-process stubs.
"""
from __future__ import annotations

import os

from apx_agent import Agent, tool

SMOKE_MODE = os.environ.get("APX_SMOKE_MODE", "0") == "1"


if SMOKE_MODE:

    @tool
    def get_recent_orders(customer_id: str) -> list[dict]:
        """Return a canned list of the customer's last 5 orders (smoke stub).

        Real deployments wire this against ``main.sales.orders`` via
        ``Dependencies.Workspace`` + ``run_sql``.
        """
        return [
            {"order_id": f"ord-{customer_id}-001", "total": 42.50,
             "status": "shipped", "ordered_at": "2026-05-12"},
            {"order_id": f"ord-{customer_id}-002", "total": 199.00,
             "status": "delivered", "ordered_at": "2026-05-05"},
            {"order_id": f"ord-{customer_id}-003", "total": 17.99,
             "status": "delivered", "ordered_at": "2026-04-28"},
        ]

    @tool
    def format_address(street: str, city: str, region: str, postal_code: str) -> str:
        """Format a postal address into a canonical multi-line string."""
        return f"{street}\n{city}, {region} {postal_code}"

    billing_tools = [get_recent_orders, format_address]

else:
    from apx_agent import Dependencies

    @tool(uc="main.agent_tools.format_address", grant=["agent_consumers"])
    def format_address(street: str, city: str, region: str, postal_code: str) -> str:
        """Format a postal address into a canonical multi-line string."""
        return f"{street}\n{city}, {region} {postal_code}"

    @tool
    def get_recent_orders(
        customer_id: str,
        ws: Dependencies.Workspace,
    ) -> list[dict]:
        """Return the customer's last 5 orders from the orders table.

        Runs as the calling user; UC grants on ``main.sales.orders`` apply.
        """
        from apx_agent import run_sql

        return run_sql(
            ws,
            "SELECT order_id, total, status, ordered_at FROM main.sales.orders "
            "WHERE customer_id = :cid ORDER BY ordered_at DESC LIMIT 5",
            parameters=[{"name": "cid", "value": customer_id, "type": "STRING"}],
        )

    billing_tools = [get_recent_orders, format_address]


agent = Agent(
    instructions=(
        "You're a billing specialist. Answer questions about invoices, charges, "
        "refunds, and payment methods. Use get_recent_orders to look up the "
        "customer's order history when relevant."
    ),
    tools=billing_tools,
)
