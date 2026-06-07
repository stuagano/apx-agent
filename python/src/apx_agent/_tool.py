"""@tool decorator — declarative entry point for agent tools.

Two forms:

  * ``@tool`` — bare. Marks a function as an apx-agent tool with no changes
    to runtime behavior. Sugar for the typed-tool pattern that already works
    when you register a plain function via ``Agent(tools=[fn])``.

  * ``@tool(uc="catalog.schema.function", grant=["principal", ...])`` —
    marks the function for synchronisation to a Unity Catalog Python
    function. After ``publish_tools_to_uc(agent)``, the function exists as
    a governed UC asset reachable from Genie, Managed MCP, other agents,
    and any UC-aware client — without redefinition.

Design rules locked at the framework level:

  1. **UC-syncable iff pure.** ``uc=...`` is rejected at decoration time if
     the function has any ``Dependencies.*`` parameter. UC functions
     execute server-side under the function owner's identity, so a
     user-scoped ``WorkspaceClient`` is unavailable inside them. Tools
     needing the calling user's identity stay Python-only and don't sync.

  2. **Python is the source of execution.** At runtime, the tool runs as
     the Python function in-process. The UC function is the discovery /
     external-composition surface (Genie, MCP, sibling agents), not the
     execution path during the LLM loop. Avoids per-tool SQL roundtrips
     and keeps debugging local.

  3. **Explicit, fully-qualified UC names.** ``uc=...`` must be a three-part
     ``catalog.schema.function`` identifier. No implicit namespacing, no
     surprise writes. The decorator raises on shorter names.

  4. **Declarative grants.** ``grant=[...]`` declares the UC principals that
     should have ``EXECUTE`` on the function. ``publish_tools_to_uc``
     enforces this — code is the source of truth for who can call the
     tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, TypeVar, overload

from ._inspection import _inspect_tool_fn
from ._resources import ResourceSpec, attach_resources

_F = TypeVar("_F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Metadata type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolMetadata:
    """Metadata attached to a function by ``@tool``.

    Stored on the wrapped callable as ``_apx_tool``. Read by
    ``publish_tools_to_uc`` to decide which tools sync to Unity Catalog
    and what grants to apply.

    Attributes:
        uc_name: Fully-qualified UC function name (``catalog.schema.fn``)
            if the function should be synced to UC; ``None`` for
            Python-only tools.
        grants: UC principals (users, groups, service principals) that
            should have ``EXECUTE`` on the synced function. Empty when
            grants are managed out-of-band or no UC sync is requested.
        description_override: Tool description supplied to the decorator,
            overriding the function's ``__doc__``. ``None`` if absent.
        name_override: Tool name supplied to the decorator, overriding the
            Python function name. ``None`` if absent.
    """

    uc_name: str | None = None
    grants: tuple[str, ...] = field(default_factory=tuple)
    description_override: str | None = None
    name_override: str | None = None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_uc_name(uc: str) -> None:
    if uc.count(".") != 2:
        raise ValueError(
            f"@tool(uc={uc!r}) must be a fully-qualified three-part UC name "
            f"of the form catalog.schema.function. Got: {uc!r}"
        )
    parts = uc.split(".")
    for part in parts:
        if not part:
            raise ValueError(
                f"@tool(uc={uc!r}) has an empty segment. Each of catalog, "
                f"schema, and function must be a non-empty identifier."
            )


def _validate_no_dependencies(fn: Callable[..., Any], uc: str) -> None:
    """Reject @tool(uc=...) on a function that uses Dependencies.*."""
    _, dep_param_names = _inspect_tool_fn(fn)
    if dep_param_names:
        raise ValueError(
            f"@tool(uc={uc!r}) cannot decorate {fn.__name__}: it has "
            f"Dependencies.* parameters {dep_param_names!r}. UC functions "
            f"execute server-side under the function owner's identity, so "
            f"user-scoped WorkspaceClient injection is unavailable. Either "
            f"remove the Dependencies parameter (and call any Databricks "
            f"APIs through the UC function's own identity) or drop uc=... "
            f"to keep the tool Python-only."
        )


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


@overload
def tool(fn: _F) -> _F: ...


@overload
def tool(
    *,
    name: str | None = ...,
    description: str | None = ...,
    uc: str | None = ...,
    grant: Iterable[str] | None = ...,
) -> Callable[[_F], _F]: ...


def tool(
    fn: _F | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    uc: str | None = None,
    grant: Iterable[str] | None = None,
) -> Any:
    """Mark a function as an apx-agent tool.

    Plain form — no args, equivalent to passing the function to
    ``Agent(tools=[...])`` directly::

        @tool
        def classify_intent(query: str) -> str:
            \"\"\"Classify a customer query as billing/technical/account/other.\"\"\"
            ...

    UC-syncable form — registers the function in Unity Catalog at deploy
    time so Genie, Managed MCP, and other agents can reach it through the
    catalog::

        @tool(uc="main.agents.tools.classify_intent", grant=["agent_consumers"])
        def classify_intent(query: str) -> str:
            \"\"\"Classify a customer query as billing/technical/account/other.\"\"\"
            ...

    Args:
        fn: The function being decorated (bare form).
        name: Override the tool name (shown to the LLM). Defaults to
            ``fn.__name__``.
        description: Override the tool description (shown to the LLM).
            Defaults to ``fn.__doc__``.
        uc: Fully-qualified UC function name (``catalog.schema.function``)
            to sync this tool into. When set, the tool is marked for
            registration by ``publish_tools_to_uc`` and contributes a
            ``DatabricksFunction`` resource at log time. Must be omitted
            for tools that use ``Dependencies.*`` parameters.
        grant: UC principals that should have ``EXECUTE`` on the synced
            function. Only meaningful when ``uc`` is set. Empty by default.

    Returns:
        The decorated function with ``_apx_tool`` metadata attached. Pass it
        to ``Agent(tools=[...])`` like any other tool.
    """

    grants_tuple = tuple(grant) if grant else ()

    def _decorate(target: _F) -> _F:
        if not callable(target):
            raise TypeError(f"@tool requires a callable; got {type(target).__name__}")

        if uc is not None:
            _validate_uc_name(uc)
            _validate_no_dependencies(target, uc)
        elif grants_tuple:
            raise ValueError(
                "@tool(grant=...) requires uc=... — grants only apply when "
                "the tool is synced to a UC function."
            )

        if name:
            target.__name__ = name
            target.__qualname__ = name
        if description:
            target.__doc__ = description

        metadata = ToolMetadata(
            uc_name=uc,
            grants=grants_tuple,
            description_override=description,
            name_override=name,
        )
        target._apx_tool = metadata  # type: ignore[attr-defined]

        # When UC-synced, the function becomes a declared resource so
        # log_agent picks it up automatically.
        if uc is not None:
            attach_resources(target, [ResourceSpec("uc_function", uc)])

        return target

    if fn is not None:
        # Bare @tool form
        return _decorate(fn)

    # @tool(...) form
    return _decorate


def get_tool_metadata(fn: Any) -> ToolMetadata | None:
    """Return ``ToolMetadata`` attached to ``fn`` by ``@tool``, or ``None``."""
    meta = getattr(fn, "_apx_tool", None)
    if isinstance(meta, ToolMetadata):
        return meta
    return None
