"""Auto-discovery — imports the user's agent_router module to register the Agent instance."""

from __future__ import annotations

import importlib
import logging

from ._models import _get_agent_instance, _set_agent_instance

logger = logging.getLogger(__name__)


def _auto_import_agent_router() -> None:
    """Import agent_router and register the module-level ``agent`` variable.

    Convention: ``agent_router.py`` lives one level up from ``core/`` and
    exposes a module-level ``agent`` variable that is a ``BaseAgent`` instance:

        {pkg}.backend.core.agent   <- this module (__name__)
        {pkg}.backend.agent_router <- discovered here; its ``agent`` is registered

    Sub-agents constructed inside ``agent_router.py`` do NOT auto-register —
    only the top-level ``agent`` assignment does. This avoids sub-agents in a
    ``SequentialAgent`` or ``ParallelAgent`` accidentally overwriting the root.
    """
    if _get_agent_instance() is not None:
        return

    from ._agents import BaseAgent  # late import to avoid circular

    # __name__ is something like "pkg.backend.core._agent._discovery"
    # We need "pkg.backend.agent_router"
    parts = __name__.split(".")
    if len(parts) >= 4:
        backend_pkg = ".".join(parts[:-3])  # go up past _agent._discovery -> core -> backend
        try:
            module = importlib.import_module(f"{backend_pkg}.agent_router")
            candidate = getattr(module, "agent", None)
            if isinstance(candidate, BaseAgent):
                _set_agent_instance(candidate)
        except ImportError:
            pass  # No agent_router.py — agent stays disabled
