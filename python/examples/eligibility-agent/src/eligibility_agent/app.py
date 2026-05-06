"""Eligibility assessment agent — apx-agent entrypoint.

Tools (in canonical reasoning order):
    get_household
    parse_documents
    compute_income
    check_residency
    assess_eligibility
    build_reasoning_trail
"""
from apx_agent import Agent, create_app

from .prompts import get_system_prompt
from .tools.get_household import get_household
from .tools.parse_documents import parse_documents
from .tools.compute_income import compute_income
from .tools.check_residency import check_residency
from .tools.assess_eligibility import assess_eligibility
from .tools.reasoning_trail import build_reasoning_trail

agent = Agent(
    tools=[
        get_household,
        parse_documents,
        compute_income,
        check_residency,
        assess_eligibility,
        build_reasoning_trail,
    ],
    instructions=get_system_prompt(),
)
app = create_app(agent)
