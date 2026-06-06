"""my_coworker — apx-agent coworker (pre-grounded data agent that remembers)."""
from __future__ import annotations

from apx_agent import CoworkerAgent


# A coworker over ``main.sales``: pre-grounded in the schema (it already
# knows the tables/columns) AND remembers across turns (facts + session).
#
# Memory upgrade path — no Lakebase required by default:
#   memory="off"        # stateless
#   memory="inmemory"   # zero infra, forgets on restart
#   memory="persistent" # (default) UC Delta tables — survives restart
#   memory="lakebase"   # production pgvector — use explicit
#                       # [tool.apx.agent.memory]/[.session] type="lakebase" blocks
agent = CoworkerAgent("main", "sales", persona='a sales analyst who knows revenue data deeply', memory="persistent", name="my_coworker")
