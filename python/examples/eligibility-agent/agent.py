"""eligibility-agent: document-based eligibility assessment with audit-grade reasoning trail.

Given an ``application_id``, the agent runs through a canonical 6-step reasoning order
(get_household -> parse_documents -> compute_income -> check_residency -> assess_eligibility
-> build_reasoning_trail) and produces an audit-grade decision trail. Tool functions live in
``tools/``; the system prompt lives in ``prompts.py``.
"""
from __future__ import annotations

from apx_agent import Agent

from prompts import get_system_prompt
from tools.assess_eligibility import assess_eligibility
from tools.check_residency import check_residency
from tools.compute_income import compute_income
from tools.get_household import get_household
from tools.parse_documents import parse_documents
from tools.reasoning_trail import build_reasoning_trail


agent = Agent(
    instructions=get_system_prompt(),
    tools=[
        get_household,
        parse_documents,
        compute_income,
        check_residency,
        assess_eligibility,
        build_reasoning_trail,
    ],
)
