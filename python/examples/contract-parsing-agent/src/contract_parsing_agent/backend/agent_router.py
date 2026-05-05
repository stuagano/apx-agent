"""Chatbot Contracts agent — tools + sub-agents wiring.

Tools: the four typed contract tools.
Sub-agents: the deployed data-inspector apx app (Delta forensics + ad-hoc SQL).
"""

from __future__ import annotations

import os

from apx_agent import Agent

from .config import get_settings
from .tools.query_portfolio import query_portfolio
from .tools.summarize_contract import summarize_contract
from .tools.find_contracts_expiring import find_contracts_expiring
from .tools.extract_new_contract import extract_new_contract


def _resolve_sub_agents() -> list[str]:
    """Allow env override (deployed URL) or fall back to config.yaml entries."""
    env = os.environ.get("DATA_INSPECTOR_URL")
    if env:
        return [env]
    return list(get_settings().sub_agents)


_settings = get_settings()

agent = Agent(
    instructions=_settings.system_prompt,
    tools=[
        query_portfolio,
        summarize_contract,
        find_contracts_expiring,
        extract_new_contract,
    ],
    sub_agents=_resolve_sub_agents(),
)
