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
            "vector_search_index", "bindings", "url", "registry", "api_prefix", "examples",
            "workflows", "data", "executor", "example")
_ROOT_ONLY = {"memory": "memory_config", "session": "session_config", "description": "_description"}


class SettingsConflict(ValueError):
    """Python and [tool.apx.agent], or a child and the root, declare different values."""


def _label(agent: BaseAgent) -> str:
    name = getattr(agent, "_name", None)
    return name if name else type(agent).__name__


def _tree(root: BaseAgent) -> list[tuple[BaseAgent, BaseAgent | None]]:
    """(agent, parent) pairs breadth-first, so a parent precedes its children.

    An agent reached through a second parent appears again, but its children are walked once."""
    from ._topology import _iter_child_agents
    from ._wiring import _agent_tool_nested_agents

    pairs: list[tuple[BaseAgent, BaseAgent | None]] = []
    seen: set[int] = set()
    queue: list[tuple[BaseAgent, BaseAgent | None]] = [(root, None)]
    while queue:
        node, parent = queue.pop(0)  # ponytail: list as queue; agent trees are small
        pairs.append((node, parent))  # a revisit is kept: resolve_settings checks it agrees
        if id(node) in seen:
            continue
        seen.add(id(node))
        queue += [(child, node) for _, child in _iter_child_agents(node)]
        queue += [(child, node) for child in _agent_tool_nested_agents(node)]
    return pairs


def resolve_settings(agent: BaseAgent, toml: AgentConfig | None) -> AgentConfig:
    """The app's effective AgentConfig. Raises SettingsConflict on a conflict.

    An object that isn't a BaseAgent (a custom or test agent) reads as declaring nothing.

    Writes each node's per-agent values that differ from the app's into
    ``node._overrides``; recomputed on every call, so calling twice is safe."""
    declared: dict[str, Any] = {
        f: getattr(agent, f, None) for f in (*PER_AGENT, *APP_WIDE) if getattr(agent, f, None) is not None
    }
    # memory/session only fill gaps: finalize_agent binds the toml's onto the agent, so no conflict check.
    declared.update({
        f: getattr(agent, attr) for f, attr in _ROOT_ONLY.items()
        if getattr(agent, attr, None) not in (None, "") and (toml is None or f not in toml.model_fields_set)
        # Against a TOML, only an explicit session= fills the session gap; a session the
        # one-word memory knob invents never beats the TOML's own default (3b-3a).
        and (f != "session" or toml is None or getattr(agent, "_session_explicit", False))
    })
    name = getattr(agent, "_name", None)
    # Validate against the toml's target: cross-field rules (e.g. sessions) depend on it.
    base = {"target": toml.target} if toml is not None else {}
    ours = AgentConfig.model_validate({"name": name if name else "agent", **base, **declared})
    if toml is None:
        config = ours
    else:
        for field in declared:
            theirs, mine = getattr(toml, field), getattr(ours, field)
            if field in toml.model_fields_set and theirs != mine:
                raise SettingsConflict(f"{field}: agent.py sets {mine!r} on agent {_label(agent)!r}; "
                                 f"[tool.apx.agent] sets {theirs!r}. Set it in one place.")
        config = toml.model_copy(update={f: getattr(ours, f) for f in declared}) if declared else toml

    from ._agents import BaseAgent

    effective: dict[int, dict[str, Any]] = {}
    for node, parent in _tree(agent):
        if parent is None:
            effective[id(node)] = {f: getattr(config, f) for f in PER_AGENT}
        else:
            for field in APP_WIDE:
                value = getattr(node, field, None)
                if value is not None and value != getattr(config, field):
                    raise SettingsConflict(f"{field} is app-wide; set it on the root agent (found on {_label(node)!r}).")
            inherited = effective[id(parent)]
            values = {
                f: getattr(node, f, None) if getattr(node, f, None) is not None else inherited[f] for f in PER_AGENT
            }
            if id(node) in effective:
                differ = [f for f in PER_AGENT if values[f] != effective[id(node)][f]]
                if differ:
                    raise SettingsConflict(f"{_label(node)!r} is reached through parents that give it different "
                                           f"{', '.join(differ)}; set it on the agent itself.")
                continue
            effective[id(node)] = values
        if not isinstance(node, BaseAgent):
            continue
        node._overrides = MappingProxyType(
            {f: v for f, v in effective[id(node)].items() if v is not None and v != getattr(config, f)}
        )
    return config
