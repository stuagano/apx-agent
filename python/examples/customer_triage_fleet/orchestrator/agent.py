"""orchestrator: Triage agent that routes customer queries to remote specialists.

Uses ``sub_agents=[$URL]`` to discover and delegate to independently deployed
billing, technical, and account specialist agents over A2A. Each specialist
serves its own ``/.well-known/agent.json`` card — the orchestrator fetches
those at startup and materializes a delegate tool per specialist.

The ``classify_intent`` tool runs locally (no remote call needed for cheap
classification). The LLM picks the right specialist delegate based on
the classification result.
"""
from __future__ import annotations

import os

from apx_agent import Agent, tool

SMOKE_MODE = os.environ.get("APX_SMOKE_MODE", "0") == "1"


if SMOKE_MODE:

    @tool
    def classify_intent(query: str) -> str:
        """Classify a customer query as billing, technical, account, or other.

        Returns one of: ``"billing"``, ``"technical"``, ``"account"``,
        ``"other"``.
        """
        q = query.lower()
        if any(w in q for w in ("bill", "invoice", "charge", "payment", "refund")):
            return "billing"
        if any(w in q for w in ("error", "bug", "broken", "crash", "not working")):
            return "technical"
        if any(w in q for w in ("password", "login", "account", "email", "username",
                                "preference", "notification", "channel")):
            return "account"
        return "other"

else:
    from apx_agent import Dependencies

    @tool(uc="main.agent_tools.classify_intent", grant=["agent_consumers"])
    def classify_intent(query: str) -> str:
        """Classify a customer query as billing, technical, account, or other."""
        q = query.lower()
        if any(w in q for w in ("bill", "invoice", "charge", "payment", "refund")):
            return "billing"
        if any(w in q for w in ("error", "bug", "broken", "crash", "not working")):
            return "technical"
        if any(w in q for w in ("password", "login", "account", "email", "username")):
            return "account"
        return "other"


agent = Agent(
    instructions=(
        "You are a customer support triage agent. Your job is to classify the "
        "user's question and route it to the correct specialist.\n"
        "\n"
        "Steps:\n"
        "1. Call classify_intent on the user's question.\n"
        "2. Based on the result, call the appropriate specialist:\n"
        "   - billing → billing_specialist\n"
        "   - technical → technical_specialist\n"
        "   - account → account_specialist\n"
        "3. If intent is 'other', answer directly with a polite acknowledgment.\n"
        "\n"
        "Always pass the user's full question to the specialist — they have "
        "the domain tools to resolve it."
    ),
    tools=[classify_intent],
    sub_agents=[
        "$BILLING_SPECIALIST_URL",
        "$TECHNICAL_SPECIALIST_URL",
        "$ACCOUNT_SPECIALIST_URL",
    ],
)
