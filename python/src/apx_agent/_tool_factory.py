"""Thin builder shared by every tool factory.

Each apx-agent tool factory (``genie_tool``, ``uc_function_tool``,
``vector_search_tool``, ...) ends with the same tail: stamp the LLM-facing
name + description onto the callable, declare the governed Mosaic AI resources
it reaches, and return it. ``build_tool`` owns that tail so factories stay
short and the conventions can't drift apart.

It is public so you can write your own governed tool in a few lines::

    from apx_agent import build_tool, run_sql, ResourceSpec, Dependencies

    def lookup_tool(table: str):
        async def _run(id: str, ws: Dependencies.Workspace) -> list[dict]:
            # Bind the LLM-controlled value; never f-string it into SQL.
            return run_sql(
                ws,
                f"SELECT * FROM {table} WHERE id = :id",
                parameters=[{"name": "id", "value": id, "type": "STRING"}],
            )
        return build_tool(
            _run,
            name="lookup",
            description=f"Look up a row in {table} by id.",
            resources=[ResourceSpec("uc_table", table)],
        )

``table`` is a SQL identifier and cannot be bound as a parameter; only
supply it from a trusted allowlist, never from LLM/user input.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Iterable

from ._resources import ResourceSpec, attach_resources
from ._tool import ExecutionIdentity, ToolMetadata, _validate_execution, get_tool_metadata

__all__ = ["build_tool", "resolve_description"]


def build_tool(
    call: Callable[..., Any],
    *,
    name: str,
    description: str,
    resources: Iterable[ResourceSpec] = (),
    execution: ExecutionIdentity | None = None,
) -> Callable[..., Any]:
    """Stamp a tool callable and declare the resources it governs.

    The common tail every factory shares. Sets the LLM-facing identity
    (``__name__`` / ``__qualname__`` / ``__doc__``) and attaches the Mosaic AI
    ``ResourceSpec`` declarations the compile path reads to mint scoped tokens.

    Args:
        call: The (async) tool function. Its workspace-client parameter, if
            any, is injected per-request by the runtime.
        name: LLM-facing tool name. Sets ``__name__`` and ``__qualname__``.
        description: LLM-facing tool description. Sets ``__doc__``.
        resources: ``ResourceSpec`` declarations the tool reaches. ``None``
            entries are skipped; an empty iterable attaches nothing (for tools
            with no fixed governed resource, e.g. an auto-discovered warehouse).
        execution: Credential identity for the operation. If omitted, Apps
            authorization infers it from dependencies and resource declarations.

    Returns:
        The same ``call`` object, stamped and resource-tagged.
    """
    _validate_execution(execution, caller="build_tool")
    call.__name__ = name
    call.__qualname__ = name
    call.__doc__ = description
    specs = [r for r in resources if r is not None]
    if specs:
        attach_resources(call, specs)
    if execution is not None:
        metadata = get_tool_metadata(call) or ToolMetadata()
        call._apx_tool = replace(metadata, execution=execution)  # type: ignore[attr-defined]
    return call


def resolve_description(
    explicit: str | None,
    *,
    uc_comment: str | None = None,
    fallback: str,
) -> str:
    """Resolve a tool description by the framework's standard precedence.

    ``explicit`` (the factory's ``description=`` argument) wins, then
    ``uc_comment`` (the Unity Catalog ``COMMENT``, when a factory fetched it),
    then ``fallback`` (the factory's generic generated description).
    """
    return explicit or uc_comment or fallback
