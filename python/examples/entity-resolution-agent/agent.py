"""entity-resolution-agent — HandoffAgent (Supervisor + Evaluator).

ADK-style top-level definition:

  * ``sub_agents/supervisor/agent.py`` — normalize + search candidates
  * ``sub_agents/evaluator/agent.py`` — fuzzy decision + decision logging
  * ``demo_data.py`` — synthetic utility accounts (used when ``DEMO_MODE=true``)

Flow:
  1. Supervisor: normalize record → vector_search across all three indexes
     (full name+address, last name+address, first name+email) or sql_search
     fallback for abnormal names → builds deduplicated candidate shortlist →
     hands off to Evaluator.
  2. Evaluator: fuzzy reasoning → enrollment decision + log → or retry
     Supervisor with search hints if confidence is below threshold.
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
