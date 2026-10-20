"""Tools declared in the project's agent.toml, referenced from Python (see
docs/design/agent-toml-source-of-truth.md). agent.toml says what platform tools
exist; platform_tools() says which agent uses them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._agents import BaseAgent, LlmAgent


@dataclass(frozen=True)
class PlatformToolRef:
    """Placeholder for agent.toml tools; resolved at startup or per request."""

    ids: frozenset[str]


def platform_tools(*ids: str) -> list[PlatformToolRef]:
    """Reference agent.toml tools by id (no ids = all). Splat into ``tools=[...]``."""
    return [PlatformToolRef(frozenset(ids))]


def platform_tool_leaves(agent: BaseAgent) -> list[tuple[LlmAgent, PlatformToolRef]]:
    """Every (LlmAgent leaf, ref) pair in the agent graph, in graph order."""
    from ._agents import LlmAgent
    from ._wiring import _collect_guardrail_targets

    return [
        (leaf, ref)
        for leaf in _collect_guardrail_targets(agent)
        if isinstance(leaf, LlmAgent)
        for ref in leaf._platform_tool_refs
    ]
