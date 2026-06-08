from __future__ import annotations

from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "serverless_stable_qh44kx_catalog",
    "payroll_demo",
    persona="a payroll analyst who knows employee compensation, pay runs, and workforce data deeply",
    memory="persistent",
    name="payroll-coworker",
)
