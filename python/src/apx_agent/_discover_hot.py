"""Hot-apply Discover wire/unwire onto the live agent (no Apps redeploy).

Discover still writes ``agent.py`` for durability. This module mutates the
same agent object ``/invocations`` closed over so Chat can use the new
sub-agent/tool on the next turn.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ._agents import LlmAgent
from ._env import resolve_env_var
from ._models import A2ASkill, AgentCard, AgentContext, AgentTool
from ._topology import _iter_child_agents
from ._ui_probe import validate_wire_peer_url

logger = logging.getLogger(__name__)


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


async def refresh_agent_context(ctx: AgentContext) -> None:
    """Rebuild tools + A2A card on the existing context (no remount)."""
    agent = ctx.agent
    tools = list(agent.collect_tools())
    remote = await agent.fetch_remote_tools()
    known = {t.name for t in tools}
    tools.extend(t for t in remote if t.name not in known)
    ctx.tools = tools
    ctx._tool_map = {t.name: t for t in tools}
    ctx.card = AgentCard(
        name=ctx.config.name,
        description=ctx.config.description,
        skills=[
            A2ASkill(
                id=t.name,
                name=t.name,
                description=t.description,
                inputSchema=t.input_schema,
                outputSchema=t.output_schema,
            )
            for t in tools
        ],
    )


def _unregister_tool(leaf: LlmAgent, tool_name: str) -> bool:
    before = len(leaf._tool_fns)
    leaf._tool_fns = [fn for fn in leaf._tool_fns if fn.__name__ != tool_name]
    leaf._analyzed = [row for row in leaf._analyzed if row[0].__name__ != tool_name]
    return len(leaf._tool_fns) < before


def _unregister_sub_agent_by_url(leaf: LlmAgent, base_url: str) -> None:
    base = base_url.rstrip("/")
    leaf._materialized_sub_agent_urls.discard(base)
    leaf._degraded_sub_agents.pop(base, None)
    # Drop callables whose matching AgentTool advertised this URL (by name scan
    # after refresh) — also drop any fn that embeds the URL in __doc__.
    drop_names: set[str] = set()
    for t in leaf.collect_tools():
        if getattr(t, "sub_agent_url", None) and str(t.sub_agent_url).rstrip("/") == base:
            drop_names.add(t.name)
    for name in drop_names:
        _unregister_tool(leaf, name)
    # Materialized delegates may not appear in collect_tools until refresh —
    # also strip by scanning fn docs / closures is fragile; fetch_remote_tools
    # is idempotent and will not re-add if URL is gone from _sub_agent_urls.


async def hot_apply_sub_agent(
    ctx: AgentContext,
    *,
    target: str,
    ref: str,
    url: str,
    env_key: str | None = None,
) -> bool:
    """Append ``ref`` to the live leaf and materialize the remote tool. Returns applied."""
    # #610 defense-in-depth: never OBO-fetch an off-allowlist peer even if a
    # caller bypasses the Discover HTTP gate.
    wire_reason = validate_wire_peer_url(url)
    if wire_reason is not None:
        logger.warning("hot-apply sub-agent rejected url: %s", wire_reason)
        return False
    leaf = resolve_live_leaf(ctx.agent, target)
    if leaf is None:
        logger.info("hot-apply sub-agent: no live leaf for target=%s", target)
        return False
    if env_key:
        os.environ[env_key] = url
    if ref not in leaf._sub_agent_urls:
        leaf._sub_agent_urls.append(ref)
    await leaf.fetch_remote_tools()
    await refresh_agent_context(ctx)
    return True


async def hot_remove_sub_agent(
    ctx: AgentContext,
    *,
    target: str,
    ref: str,
) -> bool:
    leaf = resolve_live_leaf(ctx.agent, target)
    if leaf is None:
        return False
    if ref in leaf._sub_agent_urls:
        leaf._sub_agent_urls = [u for u in leaf._sub_agent_urls if u != ref]
    resolved = resolve_env_var(ref) or ref
    if resolved and not resolved.startswith("$"):
        _unregister_sub_agent_by_url(leaf, resolved)
    await refresh_agent_context(ctx)
    return True


async def hot_apply_factory_tool(
    ctx: AgentContext,
    *,
    target: str,
    kind: str,
    binding_name: str,
    full_name: str | None = None,
    space_id: str | None = None,
    index_name: str | None = None,
    columns: list[str] | None = None,
) -> bool:
    leaf = resolve_live_leaf(ctx.agent, target)
    if leaf is None:
        logger.info("hot-apply tool: no live leaf for target=%s", target)
        return False
    if any(fn.__name__ == binding_name for fn in leaf._tool_fns):
        await refresh_agent_context(ctx)
        return True

    if kind == "uc_function":
        from .catalog import uc_function_tool

        fn = uc_function_tool(full_name or "", name=binding_name)
    elif kind == "genie_space":
        from .genie import genie_tool

        fn = genie_tool(space_id or "", name=binding_name)
    elif kind == "vector_search_index":
        from .vector_search import vector_search_tool

        fn = vector_search_tool(
            index_name or "",
            columns=columns or ["content"],
            name=binding_name,
        )
    else:
        return False

    leaf._register_tool(fn)
    await refresh_agent_context(ctx)
    return True


async def hot_remove_factory_tool(
    ctx: AgentContext,
    *,
    target: str,
    binding_name: str,
) -> bool:
    leaf = resolve_live_leaf(ctx.agent, target)
    if leaf is None:
        # Still try root if composition — unregister from every LlmAgent leaf
        removed = False
        for _name, child in _iter_child_agents(ctx.agent):
            if isinstance(child, LlmAgent):
                removed = _unregister_tool(child, binding_name) or removed
        if isinstance(ctx.agent, LlmAgent):
            removed = _unregister_tool(ctx.agent, binding_name) or removed
        if removed:
            await refresh_agent_context(ctx)
        return removed
    ok = _unregister_tool(leaf, binding_name)
    if ok:
        await refresh_agent_context(ctx)
    return ok
