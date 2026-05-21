"""entity-resolution-agent: fuzzy customer entity resolution via Supervisor + Evaluator handoff.

HandoffAgent composed of two sub-agents under ``sub_agents/``: ``supervisor`` normalizes
records and searches candidate indexes, ``evaluator`` performs fuzzy decision and logging.
``demo_data.py`` provides synthetic utility accounts when ``DEMO_MODE=true``. Supervisor
hands off candidates to Evaluator, which either commits a decision or bounces back with
search hints when confidence is below threshold.
"""
from __future__ import annotations

from apx_agent import HandoffAgent

from sub_agents.evaluator.agent import evaluator
from sub_agents.supervisor.agent import supervisor


agent = HandoffAgent(
    agents={
        "supervisor": supervisor,
        "evaluator": evaluator,
    },
    start="supervisor",
    max_handoffs=4,
)
