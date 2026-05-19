"""hot_swap_model — change a deployed agent's LLM without re-logging.

The runtime side (``_chat_agent.py::_resolve_model``) reads the
``APX_AGENT_MODEL_OVERRIDE`` env var and uses it instead of the
compile-time model. This module is the control side: writes the env
var onto a deployed serving endpoint so the next replica picks up the
new model.

Use cases:
  * Swap from ``claude-sonnet`` to ``claude-opus`` to test a more
    capable model without re-deploying the agent artifact.
  * Roll back a problematic model change in seconds (set the override
    to the previous model; agent picks it up on next call).
  * Run experiments where you'd otherwise re-log dozens of artifact
    versions just to tweak the LLM.

For traffic-split A/B testing across two complete artifacts (not just
the LLM endpoint), see ``apx_agent._canary`` instead.

Mechanism: ``ws.serving_endpoints.update_config_and_wait`` rewrites
each served entity's ``environment_vars`` to include the override.
Other env vars are preserved. The update is durable until the next
``hot_swap_model`` call or until the endpoint is redeployed.

Caveats:
  * The new env var takes effect on the next replica start. With
    scale-to-zero, the next request triggers a cold start that picks
    it up. Without scale-to-zero, existing replicas may serve the
    old model until they cycle.
  * Only affects agents that were compiled via apx-agent's
    ``compile_to_chat_agent`` / ``log_agent`` path. Hand-rolled agents
    that don't go through ``_chat_agent.py`` won't honor the override.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

# The env var name the framework runtime reads in ``_chat_agent.py``.
# Exposed as a module constant so callers can match it in their own tooling.
APX_MODEL_OVERRIDE_ENV = "APX_AGENT_MODEL_OVERRIDE"


@dataclass(frozen=True)
class HotSwapResult:
    """Outcome of a hot-swap operation."""

    endpoint_name: str
    new_model: str
    previous_model: str | None  # the value the override held before; None if first set
    served_entities_updated: int


def hot_swap_model(
    endpoint_name: str,
    model: str,
    *,
    ws: "WorkspaceClient | None" = None,
    env_var: str = APX_MODEL_OVERRIDE_ENV,
    wait: bool = True,
) -> HotSwapResult:
    """Hot-swap a deployed agent's LLM endpoint by updating env vars.

    Args:
        endpoint_name: The serving endpoint hosting the agent (the name
            ``databricks.agents.deploy`` produced).
        model: The new model serving endpoint name (e.g.
            ``databricks-claude-opus-4-7``).
        ws: WorkspaceClient. Defaults to the framework's standard
            resolution (picks up DATABRICKS_HOST / OAuth M2M env).
        env_var: Override the env var name if you've customized the
            runtime variable. Defaults to ``APX_AGENT_MODEL_OVERRIDE``.
        wait: Block until the config update completes. Default True.

    Returns:
        ``HotSwapResult`` with the previous override value (if any) so
        the caller can roll back without re-discovering it.

    Raises:
        RuntimeError: when the endpoint has no served entities to update,
            or when the SDK update call fails.
    """
    from databricks.sdk.service.serving import ServedEntityInput

    if ws is None:
        from ._defaults import _make_workspace_client
        ws = _make_workspace_client()

    current = ws.serving_endpoints.get(endpoint_name)
    config = getattr(current, "config", None)
    served_entities = getattr(config, "served_entities", None) if config else None
    if not served_entities:
        raise RuntimeError(
            f"Endpoint {endpoint_name!r} has no served entities to update. "
            "Has the agent been deployed via databricks.agents.deploy yet?"
        )

    previous_value: str | None = None
    new_served_entities: list[ServedEntityInput] = []
    for entity in served_entities:
        existing_env = dict(getattr(entity, "environment_vars", None) or {})
        if previous_value is None and env_var in existing_env:
            previous_value = existing_env[env_var]
        existing_env[env_var] = model

        new_served_entities.append(
            ServedEntityInput(
                name=getattr(entity, "name", None),
                entity_name=getattr(entity, "entity_name", None),
                entity_version=getattr(entity, "entity_version", None),
                workload_size=getattr(entity, "workload_size", None),
                workload_type=getattr(entity, "workload_type", None),
                scale_to_zero_enabled=getattr(entity, "scale_to_zero_enabled", None),
                min_provisioned_concurrency=getattr(entity, "min_provisioned_concurrency", None),
                max_provisioned_concurrency=getattr(entity, "max_provisioned_concurrency", None),
                environment_vars=existing_env,
            )
        )

    if wait:
        ws.serving_endpoints.update_config_and_wait(
            name=endpoint_name,
            served_entities=new_served_entities,
        )
    else:
        ws.serving_endpoints.update_config(
            name=endpoint_name,
            served_entities=new_served_entities,
        )

    return HotSwapResult(
        endpoint_name=endpoint_name,
        new_model=model,
        previous_model=previous_value,
        served_entities_updated=len(new_served_entities),
    )


def get_active_override(
    endpoint_name: str,
    *,
    ws: "WorkspaceClient | None" = None,
    env_var: str = APX_MODEL_OVERRIDE_ENV,
) -> str | None:
    """Return the current value of the model override env var, or None if unset.

    Useful for verifying a swap landed and for showing operators which
    model is actually serving (vs. the compile-time default).
    """
    if ws is None:
        from ._defaults import _make_workspace_client
        ws = _make_workspace_client()

    current = ws.serving_endpoints.get(endpoint_name)
    config = getattr(current, "config", None)
    served_entities = getattr(config, "served_entities", None) if config else None
    if not served_entities:
        return None

    for entity in served_entities:
        env = getattr(entity, "environment_vars", None) or {}
        if env_var in env:
            return env[env_var]
    return None
