"""Infer the credential identity required by one declared Apps operation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, get_args, get_type_hints

from fastapi import params

from ._defaults import (
    _get_principal,
    _get_progress,
    _get_request,
    _get_sql_runner,
    _get_user_client,
    _get_workspace_client,
    get_databricks_headers,
)
from ._inspection import _inspect_tool_fn
from ._resources import ResourceSpec, get_resources, get_user_api_scopes
from ._tool import ExecutionIdentity, get_tool_metadata

__all__ = ["OperationAuthorization", "infer_operation_authorization"]


@dataclass(frozen=True)
class OperationAuthorization:
    """The identity and declarations required to execute one tool operation."""

    name: str
    execution_identity: ExecutionIdentity
    requires_request_context: bool
    resources: tuple[ResourceSpec, ...]
    user_api_scopes: tuple[str, ...]


_USER_DEPENDENCIES = frozenset({_get_user_client, _get_sql_runner})
_REQUEST_CONTEXT_DEPENDENCIES = frozenset({
    get_databricks_headers,
    _get_principal,
    _get_request,
})


def _dependency_callables(
    fn: Callable[..., Any],
    dependency_names: list[str],
) -> set[Callable[..., Any]]:
    """Resolve the dependency callables for parameters `_inspect_tool_fn` identified."""
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}
    dependencies: set[Callable[..., Any]] = set()
    for name in dependency_names:
        for part in get_args(hints.get(name, Any)):
            if isinstance(part, params.Depends) and callable(part.dependency):
                dependencies.add(part.dependency)
    return dependencies


def infer_operation_authorization(fn: Callable[..., Any]) -> OperationAuthorization:
    """Infer one tool's execution identity from its declared dependencies.

    Existing dependency declarations remain the sole source of identity. A
    tool with no credential dependency defaults to user when it declares a
    resource or raw OBO scope, preserving the current fail-closed boundary.
    """
    resources = tuple(get_resources(fn))
    user_api_scopes = tuple(get_user_api_scopes(fn))
    _, dependency_names = _inspect_tool_fn(fn)
    dependencies = _dependency_callables(fn, dependency_names)

    identities: set[ExecutionIdentity] = set()
    if _get_workspace_client in dependencies:
        identities.add("service")
    if dependencies & _USER_DEPENDENCIES:
        identities.add("user")

    name = getattr(fn, "__name__", type(fn).__name__)
    if len(identities) > 1:
        raise ValueError(
            f"Tool {name!r} mixes user and service credential dependencies; "
            "split it into separate operations."
        )

    metadata = get_tool_metadata(fn)
    explicit = metadata.execution if metadata is not None else None
    inferred = next(iter(identities), None)
    if explicit is not None and inferred is not None and explicit != inferred:
        raise ValueError(
            f"Tool {name!r} declares execution={explicit!r} but dependencies "
            f"require {inferred!r}."
        )

    execution_identity: ExecutionIdentity
    if explicit is not None:
        execution_identity = explicit
    elif inferred is not None:
        execution_identity = inferred
    elif resources or user_api_scopes:
        execution_identity = "user"
    else:
        execution_identity = "service"

    return OperationAuthorization(
        name=name,
        execution_identity=execution_identity,
        requires_request_context=(
            execution_identity == "user"
            or bool(dependencies & _REQUEST_CONTEXT_DEPENDENCIES)
        ),
        resources=resources,
        user_api_scopes=user_api_scopes,
    )
