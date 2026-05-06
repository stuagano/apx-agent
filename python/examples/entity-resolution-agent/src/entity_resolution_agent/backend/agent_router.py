"""Entity resolution agent — HandoffAgent orchestrating Supervisor and Evaluator.

Flow:
  Supervisor: normalize → vector_search (default) or sql_search (initials/acronyms)
              → candidate shortlist → hand off to Evaluator
  Evaluator:  fuzzy reasoning → confidence score → log decision → report to user
              → or hand back to Supervisor with search hints if low confidence
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
