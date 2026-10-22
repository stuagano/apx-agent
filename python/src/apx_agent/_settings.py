"""apx runtime settings declared on the agent tree, merged with [tool.apx.agent].

See docs/design/agent-toml-source-of-truth.md, "Sub-project 2"."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from ._models import AgentConfig

if TYPE_CHECKING:
    from ._agents import BaseAgent

PER_AGENT = ("model", "identity")
APP_WIDE = ("target", "recovery", "gateway", "service_policies", "guardrails", "knowledge",
            "vector_search_index", "bindings", "url", "registry", "api_prefix")
_ROOT_ONLY = {"memory": "memory_config", "session": "session_config"}


def _label(agent: BaseAgent) -> str:
    name = getattr(agent, "_name", None)
    return name if name else type(agent).__name__


def _tree(root: BaseAgent) -> list[tuple[BaseAgent, BaseAgent | None]]:
    """(agent, parent) pairs breadth-first, so a parent precedes its children; each agent once."""
    from ._topology import _iter_child_agents
    from ._wiring import _agent_tool_nested_agents

    pairs: list[tuple[BaseAgent, BaseAgent | None]] = []
    seen: set[int] = set()
    queue: list[tuple[BaseAgent, BaseAgent | None]] = [(root, None)]
    while queue:
        node, parent = queue.pop(0)  # ponytail: list as queue; agent trees are small
        if id(node) in seen:
            continue
        seen.add(id(node))
        pairs.append((node, parent))
        queue += [(child, node) for _, child in _iter_child_agents(node)]
        queue += [(child, node) for child in _agent_tool_nested_agents(node)]
    return pairs


def resolve_settings(agent: BaseAgent, toml: AgentConfig | None) -> AgentConfig:
    """The app's effective AgentConfig. Raises ValueError on a conflict.

    Writes each node's per-agent values that differ from the app's into
    ``node._overrides``; recomputed on every call, so calling twice is safe."""
    declared: dict[str, Any] = {
        f: getattr(agent, f) for f in (*PER_AGENT, *APP_WIDE) if getattr(agent, f) is not None
    }
    declared.update({
        f: getattr(agent, attr) for f, attr in _ROOT_ONLY.items() if getattr(agent, attr, None) is not None
    })
    name = getattr(agent, "_name", None)
    ours = AgentConfig.model_validate({"name": name if name else "agent", **declared})
    if toml is None:
        config = ours
    else:
        for field in declared:
            theirs, mine = getattr(toml, field), getattr(ours, field)
            if field in toml.model_fields_set and theirs != mine:
                raise ValueError(f"{field}: agent.py sets {mine!r} on agent {_label(agent)!r}; "
                                 f"[tool.apx.agent] sets {theirs!r}. Set it in one place.")
        config = toml.model_copy(update={f: getattr(ours, f) for f in declared})

    effective: dict[int, dict[str, Any]] = {}
    for node, parent in _tree(agent):
        if parent is None:
            effective[id(node)] = {f: getattr(config, f) for f in PER_AGENT}
        else:
            for field in APP_WIDE:
                value = getattr(node, field)
                if value is not None and value != getattr(config, field):
                    raise ValueError(f"{field} is app-wide; set it on the root agent (found on {_label(node)!r}).")
            inherited = effective[id(parent)]
            effective[id(node)] = {
                f: getattr(node, f) if getattr(node, f) is not None else inherited[f] for f in PER_AGENT
            }
        node._overrides = MappingProxyType(
            {f: v for f, v in effective[id(node)].items() if v is not None and v != getattr(config, f)}
        )
    return config
