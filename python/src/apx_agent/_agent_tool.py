"""agent_tool — wrap any BaseAgent as a callable tool.

Mirrors Google ADK's ``AgentTool`` pattern as a first-class composition
primitive. Lets one ``LlmAgent`` delegate to another agent (local or remote)
based on the LLM's tool-calling decision, rather than via a fixed workflow
position.

Workflow agents (``SequentialAgent``, ``ParallelAgent``, ``LoopAgent``, ...)
compose agents along *deterministic* edges. ``agent_tool`` composes along
*LLM-driven* edges — the parent's LLM picks when and with what input.

Both local in-process and remote agents are supported transparently:

    # Local in-process
    specialist = Agent(tools=[lookup_lineage])
    orchestrator = Agent(tools=[agent_tool(specialist, name="data_inspector",
                                            description="Inspect table lineage")])

    # Remote (via RemoteDatabricksAgent)
    remote_billing = await RemoteDatabricksAgent.from_app_name("billing-agent")
    orchestrator = Agent(tools=[agent_tool(remote_billing, name="billing",
                                            description="Answer billing questions")])

The same wrapper handles both — ``BaseAgent.run`` is the only contract.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ._defaults import Dependencies
from ._models import Message
from ._tool_factory import build_tool

if TYPE_CHECKING:
    from ._agents import BaseAgent


def _snake_case(name: str) -> str:
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _infer_name(agent: "BaseAgent") -> str:
    explicit = getattr(agent, "_name", None)
    if explicit:
        return _snake_case(explicit)
    return _snake_case(type(agent).__name__)


def agent_tool(
    agent: "BaseAgent",
    *,
    name: str | None = None,
    description: str | None = None,
):
    """Wrap an agent as a tool callable from another ``LlmAgent``.

    The returned object is a typed Python function suitable for the
    ``tools=[...]`` parameter of ``LlmAgent``. When the parent LLM picks
    this tool, the framework invokes ``agent.run(...)`` in-process (or over
    HTTP for ``RemoteDatabricksAgent``) and returns the result.

    Args:
        agent: Any ``BaseAgent``. Local (``LlmAgent``, ``SequentialAgent``,
            ``ParallelAgent``, ``LoopAgent``, ``RouterAgent``, ``HandoffAgent``)
            or remote (``RemoteDatabricksAgent``) — the wrapper doesn't care.
        name: Tool name the parent LLM sees. Defaults to ``agent._name`` or
            snake-cased class name. Required to be descriptive — the parent
            LLM picks tools by name and description.
        description: Tool description the parent LLM sees. Strongly recommend
            providing this explicitly; the default is a generic delegate
            message which won't help the LLM decide when to call this tool.

    Returns:
        A typed function with LLM-visible parameter ``message: str``.
    """
    tool_name = name or _infer_name(agent)
    tool_desc = description or (
        f"Delegate the user's request to the {tool_name} agent. "
        "Pass the relevant question or instruction as ``message``."
    )

    async def _wrapped(message: str, request: Dependencies.Request) -> str:
        result = await agent.run(
            [Message(role="user", content=message)],
            request,
        )
        return result

    return build_tool(_wrapped, name=tool_name, description=tool_desc)
