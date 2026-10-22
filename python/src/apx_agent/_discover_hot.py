"""Resolve the live leaf agent for instruction hot-apply."""

from __future__ import annotations

from typing import Any

from ._agents import LlmAgent
from ._topology import _iter_child_agents


def resolve_live_leaf(root: Any, target: str) -> LlmAgent | None:
    """Find the live ``LlmAgent`` leaf matching a Discover ``target`` name.

    Matches Handoff/Router child keys, ``agent`` for a root LlmAgent, and
    LoopAgent's ``inner``. Returns ``None`` when the target cannot be resolved
    (e.g. Sequential ``step0`` vs source variable name) — caller falls back to
    source-only write.
    """
    target = (target or "agent").strip() or "agent"

    if isinstance(root, LlmAgent) and target == "agent":
        return root

    queue: list[tuple[str | None, Any]] = [(None, root)]
    seen: set[int] = set()
    while queue:
        name, node = queue.pop(0)
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, LlmAgent) and (name == target or (target == "agent" and name is None)):
            return node
        for child_name, child in _iter_child_agents(node):
            queue.append((child_name, child))
        # LoopAgent exposes _inner via _iter_child_agents as "inner"
        if name == target and isinstance(node, LlmAgent):
            return node

    if isinstance(root, LlmAgent):
        return root
    return None
