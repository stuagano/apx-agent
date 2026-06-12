"""Register a UC model version as a *manifest* of a deployed Databricks App.

The Apps target serves traffic from the deployed wheel / FastAPI app directly;
it does NOT load a pyfunc model from Unity Catalog. This module logs and
registers a UC model version anyway, as a **version-ledger record**: "App
``<name>`` deploy corresponds to this logged artifact". The version is tagged
``apx.serving=apps`` so downstream tooling (canary, ``apx agents list``) never
mistakes it for a serving-promoted version and never tries to
``databricks.agents.deploy`` it.

This closes the gap the model-serving deploy flagged in its own docstring —
"the auto-derived UC tags / publish-tools flow does not currently apply to
``--target apps`` (no model version to tag)". Now it does.

See ``docs/engine-scope/apps-uc-registry-shim-design.md`` for the full rationale,
including why the registered artifact is a manifest/shadow (not the executable)
and the explicit non-goals (no platform traffic split).

For the model-serving path — where the registered version IS what serves — see
``log_agent`` + ``databricks.agents.deploy`` in the deploy command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._agents import BaseAgent


@dataclass(frozen=True)
class AppsManifestResult:
    """Outcome of registering an Apps-target version manifest.

    Attributes:
        uc_name: Three-part UC name the version was registered under.
        version: The registered model version (as returned by MLflow).
        app_name: Workspace App name this version is a manifest for.
    """

    uc_name: str
    version: str
    app_name: str


# Version-level tags written on the manifest. ``apx.serving=apps`` is the
# discriminator every consumer keys on to tell a manifest version apart from a
# serving-promoted one.
SERVING_TAG = "apx.serving"
APP_NAME_TAG = "apx.apps.app_name"
BUNDLE_TARGET_TAG = "apx.apps.bundle_target"


def register_apps_manifest(
    agent: "BaseAgent",
    *,
    uc_name: str,
    model: str,
    app_name: str,
    bundle_target: str,
    agent_name: str | None = None,
    mlflow_client: Any | None = None,
) -> AppsManifestResult:
    """Log + register ``agent`` as a UC version manifest for a deployed App.

    Reuses the two serving-independent halves of the model-serving flow —
    ``log_agent`` (log + register) and ``set_uc_tags_for_agent`` (discovery
    tags) — and adds version-level ``apx.apps.*`` tags marking the version as a
    manifest. It deliberately does NOT call ``databricks.agents.deploy``: the
    App, not this artifact, serves traffic.

    Run this AFTER the App is live so a logging failure can be caught and
    surfaced without blocking the deploy (the caller is expected to treat
    exceptions as non-fatal).

    Args:
        agent: The finalized apx-agent to log.
        uc_name: Three-part UC name (``catalog.schema.model``) to register under.
        model: LLM serving endpoint name — required by ``log_agent`` to compile
            and to record on the resources list.
        app_name: Workspace App name, recorded as a version tag.
        bundle_target: DAB target the App was deployed under, recorded as a tag.
        agent_name: Friendly name for the ``apx.agent.name`` discovery tag.
            Defaults to the agent's own name inside ``set_uc_tags_for_agent``.
        mlflow_client: Optional ``MlflowClient`` (injected in tests).

    Returns:
        An ``AppsManifestResult`` with the registered version.

    Raises:
        ImportError: if mlflow isn't installed.
        Exception: anything ``log_agent`` / tag writes raise — the caller
            decides whether that's fatal (it should not be, for Apps).
    """
    import mlflow
    from mlflow.tracking import MlflowClient

    from ._chat_agent import log_agent
    from ._watchdog import set_uc_tags_for_agent

    with mlflow.start_run():
        info = log_agent(agent, model=model, registered_model_name=uc_name)
    version = str(info.registered_model_version)

    client = mlflow_client or MlflowClient()
    for key, value in (
        (SERVING_TAG, "apps"),
        (APP_NAME_TAG, app_name),
        (BUNDLE_TARGET_TAG, bundle_target),
    ):
        client.set_model_version_tag(uc_name, version, key, value)

    # Registered-model-level discovery tags (apx.agent.*) so the manifest shows
    # up in `apx agents list` / topology / watchdog, same as a serving deploy.
    set_uc_tags_for_agent(
        agent, registered_model_name=uc_name, model=model, name=agent_name,
    )

    return AppsManifestResult(uc_name=uc_name, version=version, app_name=app_name)
