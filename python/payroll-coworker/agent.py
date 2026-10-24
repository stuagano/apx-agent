from __future__ import annotations

from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "serverless_stable_qh44kx_catalog",
    "payroll_demo",
    persona="a payroll analyst who knows employee compensation, pay runs, and workforce data deeply",
    memory="persistent",
    name="payroll-coworker",
    target="responses_agent",
    description="Payroll coworker — knows employee compensation, pay runs, and workforce data.",
    model="databricks-claude-sonnet-4-6",
    examples=["Who are the top earners by department?", "Show me overtime hours by employee last pay run", "How many active vs terminated employees do we have?", "What's the total payroll cost by location?"],
)
