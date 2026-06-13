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
# Role distinguishes a soak/canary manifest version from a promoted prod one;
# GIT_SHA records the exact commit a version was deployed from (P1 provenance).
ROLE_TAG = "apx.apps.role"
GIT_SHA_TAG = "apx.apps.git_sha"
# UC alias that records which version is live in prod (P2 bookkeeping).
PROD_ALIAS = "prod"


def register_apps_manifest(
    agent: "BaseAgent",
    *,
    uc_name: str,
    model: str,
    app_name: str,
    bundle_target: str,
    agent_name: str | None = None,
    extra_version_tags: dict[str, str] | None = None,
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
        extra_version_tags: Extra version-level tags, e.g. ``{'apx.apps.role': 'canary'}``.
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

    for key, value in (extra_version_tags or {}).items():
        client.set_model_version_tag(uc_name, version, key, value)

    # Registered-model-level discovery tags (apx.agent.*) so the manifest shows
    # up in `apx agents list` / topology / watchdog, same as a serving deploy.
    set_uc_tags_for_agent(
        agent, registered_model_name=uc_name, model=model, name=agent_name,
    )

    return AppsManifestResult(uc_name=uc_name, version=version, app_name=app_name)


@dataclass(frozen=True)
class CanaryManifest:
    """A canary manifest version resolved from UC (P2 promote input).

    Attributes:
        version: The UC registered-model version string.
        git_sha: The commit the canary was deployed from (``apx.apps.git_sha``
            tag), or None if the canary was deployed from a non-git tree.
    """

    version: str
    git_sha: str | None


def find_latest_canary_version(
    uc_name: str, *, mlflow_client: Any | None = None,
) -> CanaryManifest | None:
    """Return the most recent ``apx.apps.role=canary`` manifest version, or None.

    Scans the registered model's versions, keeps those tagged as canary
    manifests, and returns the highest version number — the canary a fresh
    ``promote`` should ship. ``None`` when no canary manifest exists (the
    operator hasn't deployed a canary, or it was already promoted/torn down).
    """
    from mlflow.tracking import MlflowClient

    client = mlflow_client or MlflowClient()
    versions = client.search_model_versions(f"name='{uc_name}'")
    canaries = [
        v for v in versions
        if (getattr(v, "tags", None) or {}).get(ROLE_TAG) == "canary"
    ]
    if not canaries:
        return None
    latest = max(canaries, key=lambda v: int(v.version))
    tags = getattr(latest, "tags", None) or {}
    return CanaryManifest(version=str(latest.version), git_sha=tags.get(GIT_SHA_TAG))


def get_prod_alias_version(
    uc_name: str, *, mlflow_client: Any | None = None,
) -> str | None:
    """Return the version the ``@prod`` alias points at, or None if unset."""
    from mlflow.tracking import MlflowClient

    client = mlflow_client or MlflowClient()
    try:
        mv = client.get_model_version_by_alias(uc_name, PROD_ALIAS)
    except Exception:
        return None
    return str(mv.version) if mv is not None else None


def set_prod_alias_version(
    uc_name: str, version: str, *, mlflow_client: Any | None = None,
) -> None:
    """Point the ``@prod`` alias at ``version`` (records the live prod version)."""
    from mlflow.tracking import MlflowClient

    client = mlflow_client or MlflowClient()
    client.set_registered_model_alias(uc_name, PROD_ALIAS, version)


def get_latest_apps_version(
    uc_name: str, *, mlflow_client: Any | None = None,
) -> str | None:
    """Return the highest version number registered under ``uc_name``, or None.

    Used right after a prod re-deploy to find the version that
    ``register_apps_manifest`` just created, so the ``@prod`` alias can point at
    it. (The deploy path registers internally and doesn't surface the version.)
    """
    from mlflow.tracking import MlflowClient

    client = mlflow_client or MlflowClient()
    versions = client.search_model_versions(f"name='{uc_name}'")
    if not versions:
        return None
    return str(max(versions, key=lambda v: int(v.version)).version)


def get_version_git_sha(
    uc_name: str, version: str, *, mlflow_client: Any | None = None,
) -> str | None:
    """Return the ``apx.apps.git_sha`` tag of a specific version, or None."""
    from mlflow.tracking import MlflowClient

    client = mlflow_client or MlflowClient()
    try:
        mv = client.get_model_version(uc_name, version)
    except Exception:
        return None
    return (getattr(mv, "tags", None) or {}).get(GIT_SHA_TAG)
