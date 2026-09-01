"""Infer the credential identity required by one declared Apps operation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

from ._defaults import (
    _get_principal,
    _get_request,
    _get_sql_runner,
    _get_user_client,
    _get_workspace_client,
    get_databricks_headers,
)
from ._env import resolve_env_var
from ._inspection import _tool_dependency_callables
from ._resources import (
    ResourceSpec,
    _iter_sub_agents,
    _iter_tool_fns,
    _sub_agent_to_endpoint,
    get_resources,
    get_user_api_scopes,
    user_api_scopes_for,
)
from ._tool import ExecutionIdentity, get_tool_metadata

if TYPE_CHECKING:
    from ._agents import BaseAgent

__all__ = [
    "AppDependency",
    "AuthorizationPlan",
    "OperationAuthorization",
    "compile_authorization_plan",
    "infer_operation_authorization",
]


@dataclass(frozen=True)
class OperationAuthorization:
    """The identity and declarations required to execute one tool operation."""

    name: str
    execution_identity: ExecutionIdentity
    requires_request_context: bool
    resources: tuple[ResourceSpec, ...]
    user_api_scopes: tuple[str, ...]


@dataclass(frozen=True)
class AppDependency:
    """One Databricks App peer that requires service A2A authorization."""

    url: str


@dataclass(frozen=True)
class AuthorizationPlan:
    """Identity-partitioned Apps authorization requirements for an agent."""

    operations: tuple[OperationAuthorization, ...]
    user_resources: tuple[ResourceSpec, ...]
    service_resources: tuple[ResourceSpec, ...]
    user_api_scopes: tuple[str, ...]
    app_dependencies: tuple[AppDependency, ...]


_USER_DEPENDENCIES = frozenset({_get_user_client, _get_sql_runner})
_REQUEST_CONTEXT_DEPENDENCIES = frozenset({
    get_databricks_headers,
    _get_principal,
    _get_request,
})


def infer_operation_authorization(fn: Callable[..., Any]) -> OperationAuthorization:
    """Infer one tool's execution identity from its declared dependencies.

    Existing dependency declarations remain the sole source of identity. A
    tool with no credential dependency defaults to user when it declares a
    resource or raw OBO scope, preserving the current fail-closed boundary.
    """
    resources = tuple(get_resources(fn))
    user_api_scopes = tuple(get_user_api_scopes(fn))
    dependencies = set(_tool_dependency_callables(fn).values())

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


def compile_authorization_plan(
    agent: "BaseAgent",
    *,
    model: str,
) -> AuthorizationPlan:
    """Compile operation declarations into user and service authorization."""
    operations = tuple(sorted(
        (infer_operation_authorization(fn) for fn in _iter_tool_fns(agent)),
        key=lambda operation: (
            operation.name,
            operation.execution_identity,
            operation.requires_request_context,
            tuple(sorted(
                (resource.kind, resource.identifier)
                for resource in operation.resources
            )),
            tuple(sorted(operation.user_api_scopes)),
        ),
    ))
    user_resources: set[ResourceSpec] = set()
    service_resources: set[ResourceSpec] = {
        ResourceSpec("serving_endpoint", model),
    }
    raw_user_scopes: set[str] = set()

    for operation in operations:
        if operation.execution_identity == "user":
            user_resources.update(operation.resources)
            raw_user_scopes.update(operation.user_api_scopes)
            continue
        if operation.user_api_scopes:
            scopes = ", ".join(sorted(operation.user_api_scopes))
            raise ValueError(
                f"Tool {operation.name!r} executes as service and cannot declare "
                f"user API scopes: {scopes}."
            )
        service_resources.update(operation.resources)

    app_dependencies: set[AppDependency] = set()
    for raw in _iter_sub_agents(agent):
        resolved = resolve_env_var(raw)
        if not resolved:
            continue
        if _is_apps_https_url(resolved):
            app_dependencies.add(AppDependency(resolved))
            continue
        endpoint = _sub_agent_to_endpoint(resolved)
        if endpoint is not None:
            service_resources.add(endpoint)

    ordered_user_resources = tuple(sorted(
        user_resources,
        key=lambda resource: (resource.kind, resource.identifier),
    ))
    ordered_service_resources = tuple(sorted(
        service_resources,
        key=lambda resource: (resource.kind, resource.identifier),
    ))
    user_api_scopes = tuple(sorted({
        *user_api_scopes_for(ordered_user_resources),
        *raw_user_scopes,
    }))
    return AuthorizationPlan(
        operations=operations,
        user_resources=ordered_user_resources,
        service_resources=ordered_service_resources,
        user_api_scopes=user_api_scopes,
        app_dependencies=tuple(sorted(
            app_dependencies,
            key=lambda dependency: dependency.url,
        )),
    )


def _is_apps_https_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and (host == "databricksapps.com" or host.endswith(".databricksapps.com"))
    )
