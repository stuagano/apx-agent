"""Internal Apps host manifest for the AppKit runtime bridge."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ._agents import BaseAgent
from ._apps_authorization import (
    OperationAuthorization,
    compile_authorization_plan,
)
from ._inspection import (
    _inspect_tool_fn,
    _make_input_model,
    _schema_for_model,
    _schema_for_return,
)
from ._models import AgentConfig
from ._tool import ExecutionIdentity, ToolEffect, get_tool_metadata
from ._resources import ResourceSpec, _iter_tool_fns, user_api_scopes_for


class AppsHostResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    identifier: str


class AppsHostAppPermission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    permission: Literal["CAN_USE"] = "CAN_USE"


class AppsHostToolAnnotations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect: ToolEffect = "update"
    execution_identity: ExecutionIdentity = "user"
    requires_request_context: bool = True
    requires_user_context: bool = True


class AppsHostToolHandler(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["python"] = "python"
    ref: str


class AppsHostTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    runtime: Literal["python"] = "python"
    parameters: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    annotations: AppsHostToolAnnotations = Field(
        default_factory=AppsHostToolAnnotations
    )
    handler: AppsHostToolHandler
    resources: list[AppsHostResource] = Field(default_factory=list)
    user_api_scopes: list[str] = Field(default_factory=list)


class AppsHostAppKitLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tool_calls: int


class AppsHostAppKit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: bool = True
    tool_prefix: str = "apx."
    max_steps: int
    max_tokens: int | None = None
    limits: AppsHostAppKitLimits
    ephemeral: bool | None = None
    generation_params: dict[str, Any] | None = None


class AppsHostAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    model: str
    instructions: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    max_iterations: int


class AppsHostManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["apx.apps_host_manifest"] = "apx.apps_host_manifest"
    version: Literal[1] = 1
    agent: AppsHostAgent
    appkit: AppsHostAppKit
    tools: list[AppsHostTool]
    resources: list[AppsHostResource]
    user_resources: list[AppsHostResource]
    service_resources: list[AppsHostResource]
    app_to_app_permissions: list[AppsHostAppPermission] = Field(default_factory=list)
    user_api_scopes: list[str]


def compile_apps_host_manifest(
    agent: BaseAgent,
    config: AgentConfig | None = None,
) -> AppsHostManifest:
    """Project a finalized APX agent into the internal Apps host manifest."""
    effective = config or _agent_config_from_instance(agent)
    plan = compile_authorization_plan(agent, model=effective.model)
    operations = {operation.name: operation for operation in plan.operations}
    resources = list(dict.fromkeys((*plan.service_resources, *plan.user_resources)))
    return AppsHostManifest(
        agent=AppsHostAgent(
            name=effective.name,
            description=effective.description,
            model=effective.model,
            instructions=effective.instructions,
            temperature=effective.temperature,
            max_tokens=effective.max_tokens,
            max_iterations=effective.max_iterations,
        ),
        appkit=AppsHostAppKit(
            max_steps=effective.max_iterations,
            max_tokens=effective.max_tokens,
            limits=AppsHostAppKitLimits(max_tool_calls=effective.max_iterations),
        ),
        tools=[
            _tool_manifest(fn, operations[fn.__name__]) for fn in _iter_tool_fns(agent)
        ],
        resources=[_resource_manifest(spec) for spec in resources],
        user_resources=[_resource_manifest(spec) for spec in plan.user_resources],
        service_resources=[_resource_manifest(spec) for spec in plan.service_resources],
        app_to_app_permissions=[
            _app_permission(dependency.url) for dependency in plan.app_dependencies
        ],
        user_api_scopes=list(plan.user_api_scopes),
    )


def _agent_config_from_instance(agent: BaseAgent) -> AgentConfig:
    return AgentConfig(
        name=getattr(agent, "_name", None) or "agent",
        description=getattr(agent, "_description", "") or "",
        model=getattr(agent, "_model", None)
        or AgentConfig.model_fields["model"].default,
        instructions=getattr(agent, "_instructions", "") or "",
        temperature=getattr(agent, "_temperature", None),
        max_tokens=getattr(agent, "_max_tokens", None),
        max_iterations=getattr(agent, "_max_iterations", None)
        or AgentConfig.model_fields["max_iterations"].default,
    )


def _tool_manifest(
    fn: Any,
    authorization: OperationAuthorization,
) -> AppsHostTool:
    plain_params, _dep_names = _inspect_tool_fn(fn)
    input_model = _make_input_model(fn, plain_params)
    metadata = get_tool_metadata(fn)
    effect = metadata.effect if metadata and metadata.effect is not None else "update"
    return AppsHostTool(
        name=fn.__name__,
        description=(fn.__doc__ or "").strip(),
        parameters=_schema_for_model(input_model),
        output_schema=_schema_for_return(fn),
        annotations=AppsHostToolAnnotations(
            effect=effect,
            execution_identity=authorization.execution_identity,
            requires_request_context=authorization.requires_request_context,
            requires_user_context=authorization.execution_identity == "user",
        ),
        handler=AppsHostToolHandler(ref=f"{fn.__module__}:{fn.__qualname__}"),
        resources=[_resource_manifest(spec) for spec in authorization.resources],
        user_api_scopes=(
            sorted(
                {
                    *user_api_scopes_for(authorization.resources),
                    *authorization.user_api_scopes,
                }
            )
            if authorization.execution_identity == "user"
            else []
        ),
    )


def _resource_manifest(spec: ResourceSpec) -> AppsHostResource:
    return AppsHostResource(kind=spec.kind, identifier=spec.identifier)


def _app_permission(url: str) -> AppsHostAppPermission:
    return AppsHostAppPermission(url=url)
