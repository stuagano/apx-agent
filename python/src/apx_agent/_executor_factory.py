"""Factory function for selecting the right Executor based on AgentConfig.

Centralises the dispatch logic so callers never need to import both
executor implementations directly — they import :func:`create_executor` and
receive the right one.
"""

from __future__ import annotations

import logging
import warnings
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._agents import BaseAgent
    from ._models import AgentConfig

logger = logging.getLogger(__name__)


def resolve_executor_name(name: str) -> str:
    """Map the legacy ``'claude-sdk'`` executor name to ``'openai-compat'``.

    ``'claude-sdk'`` never meant Anthropic's Agent SDK — it is an
    OpenAI-compatible tool loop on Databricks Model Serving (#833).
    """
    if name == "claude-sdk":
        warnings.warn(
            "executor='claude-sdk' is renamed to 'openai-compat' (it is an "
            "OpenAI-compatible loop on Databricks serving, not Anthropic's "
            "Agent SDK); the old name will be removed in a future release.",
            FutureWarning,
            stacklevel=3,
        )
        return "openai-compat"
    return name


def governance_guards_present(
    agent: "BaseAgent", config: "AgentConfig | None" = None
) -> str | None:
    """Return a human-readable reason if *agent* carries governance the
    ``openai-compat`` executor cannot enforce, else ``None``.

    The ``before_tool`` / ``before_model`` / ``after_*`` hooks and the input /
    output guardrail lists are exactly what ``PolicyGate`` (human approval),
    ``GovernanceGuard``, tool allow/deny lists, rate limits, and the injection
    heuristic all compose into. The :class:`OpenAICompatExecutor` runs none of them
    — it calls the tool function directly — so selecting it silently disables
    every configured guard. ``config.guardrails`` is checked too, in case the
    declarative guards have not yet been folded onto the agent instance.
    """
    for attr in ("_before_tool", "_after_tool", "_before_model", "_after_model"):
        if getattr(agent, attr, None):
            return f"agent has a {attr.lstrip('_')} hook"
    if getattr(agent, "_timeout_s", None) is not None:
        return "agent declares timeout_s"
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

    ``'openai-compat'``
        Returns a :class:`~apx_agent._claude_sdk_executor.OpenAICompatExecutor`
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
        config = AgentConfig(model="databricks-claude-sonnet-4-6", executor="openai-compat")
        executor = create_executor(agent, config)
        # executor is an OpenAICompatExecutor
    """
    from ._agents import LlmAgent
    from ._claude_sdk_executor import OpenAICompatExecutor
    from ._langgraph_executor import LangGraphExecutor

    executor_name = resolve_executor_name(getattr(config, "executor", "langgraph") or "langgraph")

    if executor_name == "openai-compat":
        if not isinstance(agent, LlmAgent):
            logger.warning(
                "executor='openai-compat' is only supported for LlmAgent; "
                "falling back to LangGraphExecutor for %s",
                type(agent).__name__,
            )
            return LangGraphExecutor(
                agent, user_ws=ws, service_ws=service_ws, model=config.model
            )

        if reason := governance_guards_present(agent, config):
            logger.warning(
                "executor='openai-compat' cannot enforce configured governance "
                "(%s); using the governed LangGraph path instead so guards are "
                "not silently bypassed.",
                reason,
            )
            return LangGraphExecutor(
                agent, user_ws=ws, service_ws=service_ws, model=config.model
            )

        return OpenAICompatExecutor(
            model=config.model,
            tools=list(agent._tool_fns),
            instructions=agent._instructions,
            ws=ws,
        )

    # Default: langgraph
    return LangGraphExecutor(
        agent, user_ws=ws, service_ws=service_ws, model=config.model
    )
