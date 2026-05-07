"""Entity resolution agent — HandoffAgent orchestrating Supervisor and Evaluator.

Flow:
  1. Supervisor: normalize record → vector_search across all three indexes (full name+address,
     last name+address, first name+email) or sql_search fallback for abnormal names
     → builds deduplicated candidate shortlist → hands off to Evaluator
  2. Evaluator: fuzzy reasoning → enrollment decision + log → or retry Supervisor
     with search hints if confidence is below threshold

See core/supervisor.py and core/evaluator.py for tool implementations.
See docs/gold-table-design.md for the VS index and gold table schema.
"""

from apx_agent import HandoffAgent

from .core.supervisor import supervisor
from .core.evaluator import evaluator

agent = HandoffAgent(
    agents={
        "supervisor": supervisor,
        "evaluator": evaluator,
    },
    start="supervisor",
    max_handoffs=4,
)
