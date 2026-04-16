"""Standalone explain-my-bill agent using apx_agent — no APX template needed.

This is a test harness that proves apx_agent can power the same agent
that runs in the full APX scaffold. The tool functions are imported
directly from the reference project.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add the reference project's src to sys.path so we can import its agent_router
_ref_src = Path.home() / "Documents/Customers/uplight/external/explain-my-bill-agent/src"
if str(_ref_src) not in sys.path:
    sys.path.insert(0, str(_ref_src))

# Patch the reference project's core.agent and core.Dependencies to point at apx_agent.
# This way agent_router.py's `from .core.agent import Agent` and
# `from .core import Dependencies` resolve to our package.
import types

from apx_agent import Agent, Dependencies

# Create the fake module hierarchy that agent_router.py expects
_core_mod = types.ModuleType("explain_my_bill_agent.backend.core")
_core_mod.Dependencies = Dependencies  # type: ignore
sys.modules["explain_my_bill_agent.backend.core"] = _core_mod

_agent_mod = types.ModuleType("explain_my_bill_agent.backend.core.agent")
_agent_mod.Agent = Agent  # type: ignore
sys.modules["explain_my_bill_agent.backend.core.agent"] = _agent_mod

# Now import the real agent_router — its tool functions + agent registration will work
from explain_my_bill_agent.backend import agent_router  # noqa: E402

from apx_agent import AgentConfig, create_app  # noqa: E402

# Build the app using the agent from the reference project
config = AgentConfig(
    name="explain_my_bill_agent",
    description="Answer customer energy billing questions",
    model="databricks-claude-sonnet-4-6",
)

app = create_app(agent_router.agent, config=config)
