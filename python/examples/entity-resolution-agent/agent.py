"""entity-resolution-agent root agent — re-exported from the backend.

The HandoffAgent (Supervisor + Evaluator) with Vector Search lookup is
defined at ``entity_resolution_agent.backend.agent_router``. This file is
the canonical top-level import point: ``agent_server/start_server.py``
(Apps deploy) and ``apx deploy --target model-serving`` both consume
``agent`` from here.

Edit ``src/entity_resolution_agent/backend/agent_router.py`` to change
the agent's topology or tools; the re-export here stays stable.
"""
from __future__ import annotations

from entity_resolution_agent.backend.agent_router import agent  # noqa: F401
