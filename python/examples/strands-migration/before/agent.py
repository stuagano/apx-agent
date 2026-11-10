"""BEFORE: a typical Strands agent hosted on AWS Bedrock AgentCore Runtime.

Stand-in for a customer's existing agent. Not run here; it is the input to the migration.
Deployed with `agentcore launch`; AgentCore gives each session its own microVM.
"""

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

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


agent = Agent(
    model=BedrockModel(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
    tools=[lookup_order, refund_eligible],
    system_prompt=SYSTEM_PROMPT,
)

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    return {"result": str(agent(payload["prompt"]))}


if __name__ == "__main__":
    app.run()
