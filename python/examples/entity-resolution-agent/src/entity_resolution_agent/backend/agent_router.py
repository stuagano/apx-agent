"""Entity resolution agent — HandoffAgent orchestrating Supervisor and Evaluator.

Flow:
  1. Supervisor: normalize record → vector_search (default) or sql_search (fallback)
     → builds candidate shortlist → hands off to Evaluator
  2. Evaluator: fuzzy reasoning → enrollment decision + log → or retry Supervisor
     with search hints if confidence is low

See core/supervisor.py and core/evaluator.py for tool implementations.
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
