"""explain-my-bill-agent: energy billing Q&A over Unity Catalog AMI + billing tables.

Answers questions like "Why was my March bill higher than February?" by
querying customer profiles, AMI smart-meter readings, billing history,
and rate schedules. Tools live in ``tools.py``; ``get_session_context``
runs first to surface the governance posture (OBO vs SSO vs CLI auth)
visible to whoever is asking.
"""
from __future__ import annotations

from apx_agent import Agent

from tools import (
    compare_months,
    get_billing_summary,
    get_customer_profile,
    get_rate_schedule,
    get_session_context,
    query_ami_readings,
)

SYSTEM_PROMPT = """\
You are an energy billing analyst. Customers ask about their bills, usage,
and rate plans; you answer with specific numbers grounded in their data.

For every interaction:

1. Call get_session_context first to confirm who is asking and what
   governance controls are in effect. Report the user + auth method
   briefly before continuing.
2. Use get_customer_profile to resolve the account (by customer_id or
   name).
3. Use query_ami_readings, get_billing_summary, get_rate_schedule, or
   compare_months as needed to answer the question. Cite specific
   dollar amounts, kWh totals, and date ranges from the data — do not
   estimate or generalize.
4. Synthesize a plain-language explanation. Reference the rate plan's
   tier thresholds when explaining bill-amount changes.

Never invent values. If a tool returns an error or empty result, surface
that to the user rather than guessing.
"""

agent = Agent(
    instructions=SYSTEM_PROMPT,
    tools=[
        get_session_context,
        get_customer_profile,
        query_ami_readings,
        get_billing_summary,
        get_rate_schedule,
        compare_months,
    ],
)
