"""Factory function for selecting the right Executor based on AgentConfig.

Centralises the dispatch logic so callers never need to import both
executor implementations directly — they import :func:`create_executor` and
receive the right one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._agents import BaseAgent
    from ._models import AgentConfig

logger = logging.getLogger(__name__)


def governance_guards_present(
    agent: "BaseAgent", config: "AgentConfig | None" = None
) -> str | None:
    """Return a human-readable reason if *agent* carries governance the Claude
    SDK executor cannot enforce, else ``None``.

    The ``before_tool`` / ``before_model`` / ``after_*`` hooks and the input /
    output guardrail lists are exactly what ``PolicyGate`` (human approval),
    ``WatchdogGuard``, tool allow/deny lists, rate limits, and the injection
    heuristic all compose into. The :class:`ClaudeSDKExecutor` runs none of them
    — it calls the tool function directly — so selecting it silently disables
    every configured guard. ``config.guardrails`` is checked too, in case the
    declarative guards have not yet been folded onto the agent instance.
    """
    for attr in ("_before_tool", "_after_tool", "_before_model", "_after_model"):
        if getattr(agent, attr, None):
            return f"agent has a {attr.lstrip('_')} hook"
    if getattr(agent, "_input_guardrails", None):
        return "agent has input guardrails"
    if getattr(agent, "_output_guardrails", None):
        return "agent has output guardrails"
    if config is not None:
        g = config.guardrails
        if (
            g.allowed_tools is not None
            or g.blocked_tools
            or g.rate_limit is not None
            or g.injection_detection
        ):
            return "config declares [tool.apx.agent.guardrails]"
    return None


def create_executor(
    agent: "BaseAgent",
    config: "AgentConfig",
    ws: Any = None,
    service_ws: Any = None,
) -> Any:
    """Return the appropriate :class:`~apx_agent._executor.Executor` for *agent*.

    The *config.executor* field controls the dispatch:

    ``'langgraph'`` (default)
        Returns a :class:`~apx_agent._langgraph_executor.LangGraphExecutor`
        wrapping *agent*.  Works for all agent topologies.

    ``'claude-sdk'``
        Returns a :class:`~apx_agent._claude_sdk_executor.ClaudeSDKExecutor`
        populated with the tools and instructions from *agent*, but only when
        *agent* is a :class:`~apx_agent._agents.LlmAgent`.  For all other
        agent types a warning is logged and a
        :class:`~apx_agent._langgraph_executor.LangGraphExecutor` is returned
        instead — composite topologies (SequentialAgent, LoopAgent, etc.)
        require the LangGraph compilation path.

    :param agent: The :class:`~apx_agent._agents.BaseAgent` instance to wrap.
    :param config: An :class:`~apx_agent._models.AgentConfig` that carries
        the ``executor`` field (defaults to ``'langgraph'``).
    :param ws: Optional user-only :class:`~databricks.sdk.WorkspaceClient` for
        OBO auth. Never fills the LangGraph service dependency slot.
    :param service_ws: Optional app service ``WorkspaceClient``. Passed only
        to the LangGraph executor's service dependency slot.
    :returns: An executor instance whose ``run_turn`` method accepts
        the standard :class:`~apx_agent._executor.ExecutorEvent` protocol.

    Example::

        from apx_agent import LlmAgent
        from apx_agent._models import AgentConfig
        from apx_agent._executor_factory import create_executor

        agent = LlmAgent(tools=[])
        config = AgentConfig(model="databricks-claude-sonnet-4-6", executor="claude-sdk")
        executor = create_executor(agent, config)
        # executor is a ClaudeSDKExecutor
    """
    from ._agents import LlmAgent
    from ._claude_sdk_executor import ClaudeSDKExecutor
    from ._langgraph_executor import LangGraphExecutor

    executor_name = getattr(config, "executor", "langgraph") or "langgraph"

    if executor_name == "claude-sdk":
        if not isinstance(agent, LlmAgent):
            logger.warning(
                "executor='claude-sdk' is only supported for LlmAgent; "
                "falling back to LangGraphExecutor for %s",
                type(agent).__name__,
            )
            return LangGraphExecutor(
                agent, user_ws=ws, service_ws=service_ws, model=config.model
            )

        if reason := governance_guards_present(agent, config):
            logger.warning(
                "executor='claude-sdk' cannot enforce configured governance "
                "(%s); using the governed LangGraph path instead so guards are "
                "not silently bypassed.",
                reason,
            )
            return LangGraphExecutor(
                agent, user_ws=ws, service_ws=service_ws, model=config.model
            )

        return ClaudeSDKExecutor(
            model=config.model,
            tools=list(agent._tool_fns),
            instructions=agent._instructions,
            ws=ws,
        )

    # Default: langgraph
    return LangGraphExecutor(
        agent, user_ws=ws, service_ws=service_ws, model=config.model
    )
