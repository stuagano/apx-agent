"""memory-tools — agent-facing tools wrapping a :class:`MemoryStore`.

Builds three :func:`apx_agent.tool`-decorated callables (``recall`` /
``remember`` / ``forget``) so an LLM can read and write durable memory
through standard tool-calling.

``principal_id`` is resolved per-call: the optional
``principal_id_resolver`` is tried first (typically reads
request-scoped context — :class:`Dependencies.RequestContext`, OBO
headers, etc.), falling back to ``default_principal_id``. When neither
resolves, the tool degrades gracefully by returning a no-principal
string rather than raising — agents should fail soft on missing
identity, not crash the turn.

Mirrors ``typescript/src/memory-tools.ts``. No I/O lives in this module
itself; it just wires :func:`apx_agent.tool` against the injected store.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Sequence
from typing import Any, Literal

from ._defaults import PrincipalDependency  # module-level — resolvable by get_type_hints
from ._memory import MemoryStore, RecallOptions, RecallResult
from ._tool import tool

# Request-scoped principal for scoped backends (e.g. Managed Agent Memory),
# whose id-only ``delete`` needs the caller's scope. The dep-path ``forget``
# tool publishes its trusted principal here right before calling
# ``store.delete``; the store's ``scope_resolver`` reads it in the same
# synchronous frame. Mirrors the request-scoped auth contextvars in _mcp.py.
# Empty string = unset (recall/remember don't use it — they pass principal_id
# as an explicit store argument).
_PRINCIPAL_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_apx_memory_principal", default=""
)


def current_principal() -> str | None:
    """Return the request-scoped principal published by ``forget``, or ``None``."""
    return _PRINCIPAL_CTX.get() or None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


MemoryToolName = Literal["recall", "remember", "forget"]
"""Names of the tools this factory can mint."""


NO_PRINCIPAL = "No principal_id available; cannot recall memories."
"""Returned by recall / remember when no principal_id can be resolved.

Matches the TypeScript constant verbatim so agents that key off this
sentinel see the same string in both runtimes.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_recall_results(results: Sequence[RecallResult]) -> str:
    """Render recall results as a markdown bullet list.

    Each line is ``"- [score=X.XX] {content}"``. Empty input returns the
    ``"No memories found."`` sentinel so the LLM has unambiguous output.
    """
    if len(results) == 0:
        return "No memories found."
    lines: list[str] = []
    for r in results:
        lines.append(f"- [score={r.score:.2f}] {r.memory.content}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_memory_tools(
    *,
    store: MemoryStore,
    principal_id_resolver: Callable[[], str | None] | None = None,
    default_principal_id: str | None = None,
    namespace_default: str = "default",
    tool_prefix: str = "",
    include: list[MemoryToolName] | None = None,
    _use_dep_principal: bool = False,
) -> list[Any]:
    """Build LLM-facing memory tools that close over ``store``.

    Returns a list of :func:`apx_agent.tool`-decorated callables ready to
    pass into ``Agent(tools=[...])``. ``principal_id`` is resolved
    per-call: ``principal_id_resolver()`` is tried first (returning
    ``None`` falls through), then ``default_principal_id``. When neither
    yields a value, ``recall`` / ``remember`` return
    :data:`NO_PRINCIPAL` instead of raising — agents should degrade
    gracefully on missing identity.

    ``forget`` is principal-agnostic — deletion is keyed by memory id,
    not by principal — so it works even when no principal is available.

    Args:
        store: The :class:`MemoryStore` instance to wrap. Required.
        principal_id_resolver: Optional callable invoked at tool-call
            time to fetch the current ``principal_id``. Return ``None``
            to fall through to ``default_principal_id``.
        default_principal_id: Optional fallback ``principal_id`` used
            when the resolver returns ``None`` (or is absent).
        namespace_default: Namespace passed through to the store when the
            tool caller omits the ``namespace`` argument. Defaults to
            ``"default"`` — same as the store's own default.
        tool_prefix: Optional prefix prepended to every minted tool name
            (e.g. ``"memory_"`` produces ``"memory_recall"``). Useful
            when multiple stores are mounted on one agent. Defaults to
            ``""`` (no prefix).
        include: Optional subset of tool names to build. Defaults to
            all three. Passing ``["recall"]`` yields only the read tool,
            for read-only agents.
        _use_dep_principal: When ``True``, the emitted ``recall`` /
            ``remember`` tools carry a ``principal: PrincipalDependency``
            parameter so FastAPI's dependency injection supplies the
            per-request OBO identity at call time. The LLM never sees
            ``principal`` as a tool argument. Used by
            ``_memory_wiring.attach_declared_memory`` to build config-
            driven memory tools. When ``False`` (default), the classic
            zero-arg resolver path is used.

    Returns:
        A list of decorated callables, in the order requested by
        ``include`` (or ``[recall, remember, forget]`` by default).
    """
    requested: list[MemoryToolName] = (
        list(include) if include is not None else ["recall", "remember", "forget"]
    )

    def _resolve_principal() -> str | None:
        """Run resolver-then-default lookup — once per tool call."""
        if principal_id_resolver is not None:
            from_resolver = principal_id_resolver()
            if from_resolver:
                return from_resolver
        return default_principal_id

    tools: list[Any] = []

    # The dep / resolver branches below use distinct inner function names
    # (_recall_dep vs _recall_resolver, etc.) so pyright doesn't flag a
    # reportRedeclaration; @tool(name=...) sets the tool name the LLM sees.
    for name in requested:
        if name == "recall":
            if _use_dep_principal:

                @tool(name=f"{tool_prefix}recall")
                def _recall_dep(
                    query: str,
                    k: int = 5,
                    namespace: str | None = None,
                    tags: list[str] | None = None,
                    *,
                    principal: PrincipalDependency,
                ) -> str:
                    """Recall durable memories relevant to a query.

                    Returns the top-k matches formatted as a markdown bullet
                    list. Each line is ``"- [score=X.XX] {content}"``.
                    Returns ``"No memories found."`` when nothing matches.
                    """
                    # Per-request OBO principal wins; fall back to the configured
                    # default (e.g. the local CLI-profile identity) so memory
                    # works when no OBO header is present (local apx-agent run).
                    principal = principal or default_principal_id
                    if not principal:
                        return NO_PRINCIPAL
                    results = store.recall(
                        RecallOptions(
                            principal_id=principal,
                            query=query,
                            k=k,
                            namespace=namespace if namespace is not None else namespace_default,
                            tags=tuple(tags) if tags is not None else None,
                        )
                    )
                    return _format_recall_results(results)

                tools.append(_recall_dep)

            else:

                @tool(name=f"{tool_prefix}recall")
                def _recall_resolver(
                    query: str,
                    k: int = 5,
                    namespace: str | None = None,
                    tags: list[str] | None = None,
                ) -> str:
                    """Recall durable memories relevant to a query.

                    Returns the top-k matches formatted as a markdown bullet
                    list. Each line is ``"- [score=X.XX] {content}"``.
                    Returns ``"No memories found."`` when nothing matches.
                    """
                    principal_id = _resolve_principal()
                    if not principal_id:
                        return NO_PRINCIPAL
                    results = store.recall(
                        RecallOptions(
                            principal_id=principal_id,
                            query=query,
                            k=k,
                            namespace=namespace if namespace is not None else namespace_default,
                            tags=tuple(tags) if tags is not None else None,
                        )
                    )
                    return _format_recall_results(results)

                tools.append(_recall_resolver)

        elif name == "remember":
            if _use_dep_principal:

                @tool(name=f"{tool_prefix}remember")
                def _remember_dep(
                    content: str,
                    namespace: str | None = None,
                    tags: list[str] | None = None,
                    importance: float = 0.5,
                    *,
                    principal: PrincipalDependency,
                ) -> str:
                    """Save a durable memory for later recall.

                    Persists ``content`` under the current principal and
                    returns the new memory id. ``importance`` is a 0..1
                    hint used by the store for ranking and pruning.
                    """
                    if not (0.0 <= importance <= 1.0):
                        raise ValueError(
                            f"importance must be in [0, 1]; got {importance}"
                        )
                    # Per-request OBO principal wins; fall back to the configured
                    # default (e.g. the local CLI-profile identity) so memory
                    # works when no OBO header is present (local apx-agent run).
                    principal = principal or default_principal_id
                    if not principal:
                        return NO_PRINCIPAL
                    memory = store.add(
                        {
                            "principal_id": principal,
                            "content": content,
                            "namespace": namespace if namespace is not None else namespace_default,
                            "tags": tuple(tags) if tags is not None else (),
                            "importance": importance,
                        }
                    )
                    return f"Saved memory {memory.id}."

                tools.append(_remember_dep)

            else:

                @tool(name=f"{tool_prefix}remember")
                def _remember_resolver(
                    content: str,
                    namespace: str | None = None,
                    tags: list[str] | None = None,
                    importance: float = 0.5,
                ) -> str:
                    """Save a durable memory for later recall.

                    Persists ``content`` under the current principal and
                    returns the new memory id. ``importance`` is a 0..1
                    hint used by the store for ranking and pruning.
                    """
                    if not (0.0 <= importance <= 1.0):
                        raise ValueError(
                            f"importance must be in [0, 1]; got {importance}"
                        )
                    principal_id = _resolve_principal()
                    if not principal_id:
                        return NO_PRINCIPAL
                    memory = store.add(
                        {
                            "principal_id": principal_id,
                            "content": content,
                            "namespace": namespace if namespace is not None else namespace_default,
                            "tags": tuple(tags) if tags is not None else (),
                            "importance": importance,
                        }
                    )
                    return f"Saved memory {memory.id}."

                tools.append(_remember_resolver)

        elif name == "forget":
            if _use_dep_principal:

                @tool(name=f"{tool_prefix}forget")
                def _forget_dep(id: str, *, principal: PrincipalDependency) -> str:
                    """Delete a memory by id.

                    Returns ``"Forgot memory {id}."`` on hit or
                    ``"No memory with id {id}."`` on miss.
                    """
                    # Publish the trusted per-request principal so scoped
                    # backends (Managed Agent Memory) resolve the caller's own
                    # scope for this id-only delete — never a model-supplied
                    # scope. Set in the same sync frame store.delete runs in.
                    _PRINCIPAL_CTX.set((principal or default_principal_id) or "")
                    deleted = store.delete(id)
                    if deleted:
                        return f"Forgot memory {id}."
                    return f"No memory with id {id}."

                tools.append(_forget_dep)

            else:

                @tool(name=f"{tool_prefix}forget")
                def forget(id: str) -> str:
                    """Delete a memory by id.

                    Returns ``"Forgot memory {id}."`` on hit or
                    ``"No memory with id {id}."`` on miss. Principal-agnostic
                    — operates by id only.
                    """
                    deleted = store.delete(id)
                    if deleted:
                        return f"Forgot memory {id}."
                    return f"No memory with id {id}."

                tools.append(forget)

        else:  # pragma: no cover - guarded by Literal type
            raise ValueError(f"unknown memory tool name: {name!r}")

    return tools


__all__ = [
    "MemoryToolName",
    "NO_PRINCIPAL",
    "current_principal",
    "make_memory_tools",
]
