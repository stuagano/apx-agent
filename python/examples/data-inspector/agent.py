"""data-inspector root agent — re-exported from the backend.

The LlmAgent with Delta-forensics + UC discovery tools is defined at
``data_inspector.backend.agent_router``. This file is the canonical
top-level import point: ``agent_server/start_server.py`` (Apps deploy)
and ``apx deploy --target model-serving`` both consume ``agent`` from
here.

Edit ``src/data_inspector/backend/agent_router.py`` to change the agent's
tools or instructions; the re-export here stays stable.
"""
from __future__ import annotations

from data_inspector.backend.agent_router import agent  # noqa: F401
