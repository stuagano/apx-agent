"""Client-side deferred tool loading for the compiled LangGraph path (#767).

``LlmAgent(tool_loading="deferred")`` still registers every author tool on
``create_agent`` so ``ToolNode`` can execute a revealed call. Middleware hides
those tools from the *model* until ``tool_search`` writes their names into the
keyed ``state["bound_tools"]`` channel. Extra orchestration tools (Loop
``finish_loop``, Handoff ``transfer_to_*``) stay always visible.

This is a client-side router, not Anthropic's server-side tool-search beta.
Ranking is substring / token overlap on name + description — good enough for
the served ACs, no extra dependency.
"""

from __future__ import annotations

from typing import Any

from ._defaults import Dependencies
from ._inspection import _inspect_tool_fn, _make_input_model, _schema_for_model

TOOL_SEARCH_NAME = "tool_search"
STATE_KEY = "bound_tools"
_VALID_LOADING = frozenset({"eager", "deferred"})


def is_deferred(agent: Any) -> bool:
    """True when ``agent`` was constructed with ``tool_loading="deferred"``."""
    return hasattr(agent, "_tool_loading") and agent._tool_loading == "deferred"


def validate_tool_loading(value: str) -> str:
    """Accept only ``eager`` / ``deferred``; raise ``ValueError`` otherwise."""
    if value not in _VALID_LOADING:
        raise ValueError(
            f"tool_loading must be 'eager' or 'deferred', got {value!r}"
        )
    return value


def catalog_entry(fn: Any) -> dict[str, Any]:
    """LLM-visible schema for one author tool (name, description, parameters)."""
    signature = _inspect_tool_fn(fn)
    input_model = _make_input_model(fn, signature.plain_params)
    return {
        "name": fn.__name__,
        "description": (fn.__doc__ or "").strip(),
        "parameters": _schema_for_model(input_model),
    }


def rank_tools(query: str, catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank catalog entries by substring / token overlap on name + description.

    Not BM25. An empty query matches nothing. Ties break on name.
    """
    q = query.strip().lower()
    if not q:
        return []
    tokens = [tok for tok in q.replace("_", " ").split() if tok]
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in catalog:
        hay = f"{entry.get('name', '')} {entry.get('description', '')}".lower()
        score = 0
        if q in hay:
            score += 2
        score += sum(1 for tok in tokens if tok in hay)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("name", ""))))
    return [entry for _, entry in scored]


def make_tool_search_fn(tool_fns: list[Any]) -> Any:
    """Build the compile-injected ``tool_search`` tool over ``tool_fns``.

    Catalog is snapshotted at compile time from the current author tool list
    (so ``DataAgent.bind_workspace`` late-registers are included). The
    function is *not* appended to ``agent._tool_fns`` — eager agents must not
    advertise it, and deferred agents must not double-register it.
    """
    catalog = [catalog_entry(fn) for fn in tool_fns]

    def tool_search(
        query: str,
        state: Dependencies.State,
    ) -> list[dict[str, Any]]:
        """Search the agent's tool catalog and bind matching tools for later hops.

        Call this when you need a capability that is not in the current tool
        list. Matching tools stay bound for the rest of the session.
        """
        matched = rank_tools(query, catalog)
        names = [entry["name"] for entry in matched if entry.get("name")]
        if not names:
            return matched
        already = list(state.get(STATE_KEY) or [])
        merged = list(already)
        for name in names:
            if name not in merged:
                merged.append(name)
        # Reassign — in-place list mutation is not tracked by StateProxy.
        state[STATE_KEY] = merged
        return matched

    tool_search.__name__ = TOOL_SEARCH_NAME
    tool_search.__qualname__ = TOOL_SEARCH_NAME
    return tool_search


def _bound_names(request: Any) -> set[str]:
    """Names previously revealed into the keyed ``state`` channel."""
    graph_state = request.state if hasattr(request, "state") else None
    keyed: Any = {}
    if graph_state is not None and hasattr(graph_state, "get"):
        keyed = graph_state.get("state") or {}
    names = keyed.get(STATE_KEY) or [] if isinstance(keyed, dict) else []
    return {name for name in names if isinstance(name, str)}


def deferred_tools_middleware(
    *,
    searchable_tools: list[Any],
    search_tool: Any,
    always_visible: list[Any] | None = None,
) -> Any:
    """Filter model-visible tools each hop; ``ToolNode`` still has the inventory.

    Implements both sync and async wrap hooks — a sync-only middleware raises
    ``NotImplementedError`` on the async served path (#243).
    """
    from langchain.agents.middleware import AgentMiddleware

    pinned = list(always_visible or [])

    def _visible(request: Any) -> list[Any]:
        bound = _bound_names(request)
        revealed = [
            tool
            for tool in searchable_tools
            if hasattr(tool, "name") and tool.name in bound
        ]
        return [search_tool, *pinned, *revealed]

    class _DeferredToolsMiddleware(AgentMiddleware):
        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(request.override(tools=_visible(request)))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(request.override(tools=_visible(request)))

    return _DeferredToolsMiddleware()
