"""Internal Apps host manifest for the AppKit runtime bridge."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from urllib.parse import urlparse

from ._agents import BaseAgent
from ._inspection import (
    _inspect_tool_fn,
    _make_input_model,
    _schema_for_model,
    _schema_for_return,
)
from ._models import AgentConfig
from ._resources import (
    ResourceSpec,
    _iter_sub_agents,
    _iter_tool_fns,
    collect_resource_specs,
    collect_user_api_scopes,
    get_resources,
    get_user_api_scopes,
    user_api_scopes_for,
)


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

    effect: str = "read"
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
    app_to_app_permissions: list[AppsHostAppPermission] = Field(default_factory=list)
    user_api_scopes: list[str]


def compile_apps_host_manifest(
    agent: BaseAgent,
    config: AgentConfig | None = None,
) -> AppsHostManifest:
    """Project a finalized APX agent into the internal Apps host manifest."""
    effective = config or _agent_config_from_instance(agent)
    resources = collect_resource_specs(agent, model=effective.model)
    scopes = sorted(
        {
            *user_api_scopes_for(resources),
            *collect_user_api_scopes(agent),
        }
    )
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
        tools=[_tool_manifest(fn) for fn in _iter_tool_fns(agent)],
        resources=[_resource_manifest(spec) for spec in resources],
        app_to_app_permissions=[
            _app_permission(url) for url in _iter_apps_peer_urls(agent)
        ],
        user_api_scopes=scopes,
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


def _tool_manifest(fn: Any) -> AppsHostTool:
    plain_params, _dep_names = _inspect_tool_fn(fn)
    input_model = _make_input_model(fn, plain_params)
    resources = get_resources(fn)
    scopes = sorted(
        {
            *user_api_scopes_for(resources),
            *get_user_api_scopes(fn),
        }
    )
    return AppsHostTool(
        name=fn.__name__,
        description=(fn.__doc__ or "").strip(),
        parameters=_schema_for_model(input_model),
        output_schema=_schema_for_return(fn),
        handler=AppsHostToolHandler(ref=f"{fn.__module__}:{fn.__qualname__}"),
        resources=[_resource_manifest(spec) for spec in resources],
        user_api_scopes=scopes,
    )


def _resource_manifest(spec: ResourceSpec) -> AppsHostResource:
    return AppsHostResource(kind=spec.kind, identifier=spec.identifier)


def _iter_apps_peer_urls(agent: BaseAgent) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in _iter_sub_agents(agent):
        if not _is_apps_https_url(raw) or raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    return out


def _is_apps_https_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == "databricksapps.com" or host.endswith(".databricksapps.com")


def _app_permission(url: str) -> AppsHostAppPermission:
    return AppsHostAppPermission(url=url)
