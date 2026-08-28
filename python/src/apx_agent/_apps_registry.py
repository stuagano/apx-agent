"""Register a UC model version as a *manifest* of a deployed Databricks App.

The Apps target serves traffic from the deployed wheel / FastAPI app directly;
it does NOT load a pyfunc model from Unity Catalog. This module logs and
registers a minimal UC model version anyway, as a **version-ledger record**: "App
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

import contextlib
import os
import time
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
# Provenance stamped on EVERY deploy (issue #403): whether the deploying tree
# had uncommitted changes, and the sha256 of the uv.lock that shipped — so
# "does what's deployed match my working tree?" is answerable after the fact.
GIT_DIRTY_TAG = "apx.git_dirty"
LOCK_SHA256_TAG = "apx.lock_sha256"
# UC alias that records which version is live in prod (P2 bookkeeping).
PROD_ALIAS = "prod"


@contextlib.contextmanager
def _uc_registry_context(profile: str | None = None):
    import mlflow

    old_tracking_uri = mlflow.get_tracking_uri()
    old_registry_uri = mlflow.get_registry_uri()
    tracking_uri = f"databricks://{profile}" if profile else "databricks"
    registry_uri = f"databricks-uc://{profile}" if profile else "databricks-uc"
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_registry_uri(registry_uri)
    try:
        yield
    finally:
        mlflow.set_tracking_uri(old_tracking_uri)
        mlflow.set_registry_uri(old_registry_uri)


def _looks_like_uc_visibility_delay(error: Exception) -> bool:
    message = str(error).lower()
    return (
        "not found" in message
        or "does not exist" in message
        or "resource_does_not_exist" in message
    )


def uc_safe_tag_key(key: str) -> str:
    """Return a UC model-version tag key accepted by the registry API."""
    return key.replace(".", "_").replace("=", "_")


def tag_value(tags: dict[str, str], key: str) -> str | None:
    """Read a tag using either the historical dotted key or UC-safe key."""
    return tags.get(key) or tags.get(uc_safe_tag_key(key))


def _set_model_version_tag_with_retry(
    client: Any,
    uc_name: str,
    version: str,
    key: str,
    value: str,
    *,
    attempts: int = 6,
    delay_seconds: float = 2.0,
) -> None:
    for attempt in range(attempts):
        try:
            client.set_model_version_tag(
                uc_name, version, uc_safe_tag_key(key), value,
            )
            return
        except Exception as e:
            if attempt == attempts - 1 or not _looks_like_uc_visibility_delay(e):
                raise
            time.sleep(delay_seconds)


def register_apps_manifest(
    agent: "BaseAgent",
    *,
    uc_name: str,
    model: str,
    app_name: str,
    bundle_target: str,
    agent_name: str | None = None,
    extra_version_tags: dict[str, str] | None = None,
    experiment_id: str | None = None,
    mlflow_client: Any | None = None,
    profile: str | None = None,
) -> AppsManifestResult:
    """Log + register ``agent`` as a UC version manifest for a deployed App.

    Logs a tiny non-serving pyfunc artifact, calls ``set_uc_tags_for_agent`` for
    discovery tags, and adds version-level ``apx.apps.*`` tags marking the
    version as a manifest. It deliberately does NOT call
    ``databricks.agents.deploy`` or exercise the live agent: the App, not this
    artifact, serves traffic.

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
        experiment_id: Optional Databricks MLflow experiment id for the
            manifest run. When omitted, ``MLFLOW_EXPERIMENT_ID`` is honored.
        mlflow_client: Optional ``MlflowClient`` (injected in tests).
        profile: Optional Databricks CLI profile for MLflow tracking/registry
            auth. When set, MLflow uses ``databricks://<profile>`` and
            ``databricks-uc://<profile>`` instead of ambient credentials.

    Returns:
        An ``AppsManifestResult`` with the registered version.

    Raises:
        ImportError: if mlflow isn't installed.
        Exception: anything ``log_agent`` / tag writes raise — the caller
            decides whether that's fatal (it should not be, for Apps).
    """
    import mlflow
    from mlflow.tracking import MlflowClient
    from mlflow.models import ModelSignature
    from mlflow.types.schema import ColSpec, Schema

    from ._watchdog import set_uc_tags_for_agent

    pyfunc: Any = mlflow.pyfunc

    class _AppsManifestModel(pyfunc.PythonModel):
        def predict(self, context, model_input, params=None):
            return [{"app_name": app_name, "apx.serving": "apps"}]

    old_tracking_uri = mlflow.get_tracking_uri()
    old_registry_uri = mlflow.get_registry_uri()
    signature = ModelSignature(
        inputs=Schema([ColSpec("string", "input")]),
        outputs=Schema([
            ColSpec("string", "app_name"),
            ColSpec("string", "apx.serving"),
        ]),
    )
    tracking_uri = f"databricks://{profile}" if profile else "databricks"
    registry_uri = f"databricks-uc://{profile}" if profile else "databricks-uc"
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_registry_uri(registry_uri)
    try:
        with mlflow.start_run(
            experiment_id=experiment_id or os.environ.get("MLFLOW_EXPERIMENT_ID"),
        ) as run:
            info = mlflow.pyfunc.log_model(
                artifact_path="apps_manifest",
                python_model=_AppsManifestModel(),
                registered_model_name=uc_name,
                signature=signature,
                pip_requirements=[],
                metadata={
                    "apx.serving": "apps",
                    "apx.apps.app_name": app_name,
                    "apx.apps.bundle_target": bundle_target,
                },
            )
            if info is None or getattr(info, "registered_model_version", None) is None:
                info = mlflow.register_model(
                    f"runs:/{run.info.run_id}/apps_manifest", uc_name,
                )
        info_any: Any = info
        version = str(info_any.registered_model_version)

        client = mlflow_client or MlflowClient()
        for key, value in (
            (SERVING_TAG, "apps"),
            (APP_NAME_TAG, app_name),
            (BUNDLE_TARGET_TAG, bundle_target),
        ):
            _set_model_version_tag_with_retry(client, uc_name, version, key, value)

        for key, value in (extra_version_tags or {}).items():
            _set_model_version_tag_with_retry(client, uc_name, version, key, value)

        # Registered-model-level discovery tags (apx.agent.*) so the manifest
        # shows up in `apx agents list` / topology / watchdog, same as a serving
        # deploy.
        set_uc_tags_for_agent(
            agent, registered_model_name=uc_name, model=model, name=agent_name,
        )

        return AppsManifestResult(uc_name=uc_name, version=version, app_name=app_name)
    finally:
        mlflow.set_tracking_uri(old_tracking_uri)
        mlflow.set_registry_uri(old_registry_uri)


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

    with _uc_registry_context():
        client = mlflow_client or MlflowClient()
        version_summaries = client.search_model_versions(f"name='{uc_name}'")
        versions = [
            client.get_model_version(uc_name, str(v.version))
            for v in version_summaries
        ]
        canaries = [
            v for v in versions
            if tag_value(getattr(v, "tags", None) or {}, ROLE_TAG) == "canary"
        ]
        if not canaries:
            return None
        latest = max(canaries, key=lambda v: int(v.version))
        tags = getattr(latest, "tags", None) or {}
        return CanaryManifest(
            version=str(latest.version),
            git_sha=tag_value(tags, GIT_SHA_TAG),
        )


def get_prod_alias_version(
    uc_name: str, *, mlflow_client: Any | None = None,
) -> str | None:
    """Return the version the ``@prod`` alias points at, or None if unset."""
    from mlflow.tracking import MlflowClient

    with _uc_registry_context():
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

    with _uc_registry_context():
        client = mlflow_client or MlflowClient()
        client.set_registered_model_alias(uc_name, PROD_ALIAS, version)


def get_latest_prod_version(
    uc_name: str, *, mlflow_client: Any | None = None,
) -> str | None:
    """Return the highest version tagged ``apx.apps.role=prod``, or None.

    Unlike ``get_latest_apps_version`` (max over ALL roles), this considers only
    prod manifests. Used after a prod re-deploy to move ``@prod``: if the deploy's
    manifest registration didn't create a new prod version, this returns the
    previous prod version (not the canary), so ``@prod`` is never pointed at a
    canary version.
    """
    from mlflow.tracking import MlflowClient

    with _uc_registry_context():
        client = mlflow_client or MlflowClient()
        version_summaries = client.search_model_versions(f"name='{uc_name}'")
        versions = [
            client.get_model_version(uc_name, str(v.version))
            for v in version_summaries
        ]
        prods = [
            v for v in versions
            if tag_value(getattr(v, "tags", None) or {}, ROLE_TAG) == "prod"
        ]
        if not prods:
            return None
        return str(max(prods, key=lambda v: int(v.version)).version)


def get_latest_apps_version(
    uc_name: str, *, mlflow_client: Any | None = None,
) -> str | None:
    """Return the highest version number registered under ``uc_name``, or None.

    Used right after a prod re-deploy to find the version that
    ``register_apps_manifest`` just created, so the ``@prod`` alias can point at
    it. (The deploy path registers internally and doesn't surface the version.)
    """
    from mlflow.tracking import MlflowClient

    with _uc_registry_context():
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

    with _uc_registry_context():
        client = mlflow_client or MlflowClient()
        try:
            mv = client.get_model_version(uc_name, version)
        except Exception:
            return None
        return tag_value(getattr(mv, "tags", None) or {}, GIT_SHA_TAG)
