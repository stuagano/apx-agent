"""slack-agent root agent — re-exported from the backend.

The LlmAgent (with ``who_am_i`` OBO probe) is defined at
``slack_agent.backend.agent_router``. This file is the canonical
top-level import point: ``agent_server/start_server.py`` (Apps deploy)
and ``apx deploy --target model-serving`` both consume ``agent`` from
here.

Slack-specific routing (signature validation, OAuth callback, user
identity exchange) is mounted as a FastAPI router by
``agent_server/start_server.py`` — it imports
``slack_agent.backend.slack_router`` directly.

Edit ``src/slack_agent/backend/agent_router.py`` to change the agent's
tools or instructions; the re-export here stays stable.
"""
from __future__ import annotations

from slack_agent.backend.agent_router import agent  # noqa: F401
