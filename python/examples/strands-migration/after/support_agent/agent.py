"""AFTER: the same Strands agent. Tools and prompt are unchanged; only the model and hosting move.

Hosting moved to runtime/ (DurableAgentServer). The agent is built per request so it can load the
session's history and get a fresh model token.
"""

from strands import Agent, tool

# Fake order system; a real agent would call an API or query a table.
ORDERS = {
    "A100": {"item": "trail shoes", "status": "delivered", "days_since_delivery": 12},
    "A200": {"item": "rain jacket", "status": "in transit", "days_since_delivery": None},
}
REFUND_WINDOW_DAYS = 30

SYSTEM_PROMPT = (
    "You are an order-support agent. Use the tools to look up orders and the refund policy. "
    "Answer in two sentences or fewer."
)


@tool
def lookup_order(order_id: str) -> dict:
    """Look up an order by its ID.

    Args:
        order_id: The order ID, for example A100.
    """
    return ORDERS.get(order_id.upper(), {"error": f"no order {order_id}"})


@tool
def refund_eligible(days_since_delivery: int) -> bool:
    """Return True if an order delivered this many days ago can still be refunded.

    Args:
        days_since_delivery: Days since the order was delivered.
    """
    return days_since_delivery <= REFUND_WINDOW_DAYS


def build_agent(model, messages: list | None = None) -> Agent:
    return Agent(
        model=model,
        tools=[lookup_order, refund_eligible],
        system_prompt=SYSTEM_PROMPT,
        messages=messages or [],
        callback_handler=None,  # the runtime streams events itself
    )
