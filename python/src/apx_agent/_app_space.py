"""Native App Space validation and SDK-owned Runtime Store binding."""

from __future__ import annotations

from typing import Any
from pathlib import Path

import click

RUNTIME_STORE_VARIABLES = {
    "DATABRICKS_AGENTBRICKS_RUNTIME_STORE_" + suffix: "apx_runtime_store_" + suffix.lower()
    for suffix in ("LAKEBASE_BRANCH", "DATABASE", "USERNAME", "SCHEMA")
}


def validate_space_env(env: list[dict[str, Any]]) -> None:
    forbidden = {"PIP_INDEX_URL", "UV_INDEX_URL", "UV_DEFAULT_INDEX",
                 "DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LAKEBASE_ENDPOINT"}
    if any(entry.get("name") in forbidden for entry in env):
        raise click.ClickException("Remove local/legacy Runtime Store and package-index overrides for App Space deployment.")


def validate_space_source(cwd: Path, app: dict[str, Any]) -> None:
    """Source configuration must not replace the validated bundle binding."""
    import yaml

    validate_space_env(app.get("config", {}).get("env", []))
    source = app.get("source_code_path")
    if not isinstance(source, str) or "${" in source:
        raise click.ClickException("App Space requires a literal source_code_path.")
    for name in ("app.yaml", "app.yml"):
        path = cwd / source / name
        if path.exists():
            config = yaml.safe_load(path.read_text()) or {}
            env = config.get("env", [])
            validate_space_env(env)
            if any(str(entry.get("name")).startswith("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_")
                   or entry.get("name") == "APX_APPS_HOST" for entry in env):
                raise click.ClickException("Put the host selector and Runtime Store binding in databricks.yml, not source app.yaml/app.yml.")


def validate_space_bundle(doc: dict[str, Any], *, bundle_key: str) -> str:
    """Offline App Space bundle-shape checks; returns the declared space name."""
    app = doc["resources"]["apps"][bundle_key]
    space = app.get("space")
    if not isinstance(space, str) or not space or "${" in space:
        raise click.ClickException("App Space must be a literal non-empty name in the root app declaration.")
    if doc.get("include") or doc.get("permissions"):
        raise click.ClickException("App Space deployment requires a self-contained bundle without bundle-level permissions.")
    if any(key in app for key in ("compute_size", "compute_min_instances", "compute_max_instances")):
        raise click.ClickException("App Space selects its compute; remove dedicated-instance scaling fields.")
    if any(app.get(key) for key in ("resources", "user_api_scopes", "permissions")):
        raise click.ClickException("App Space permissions are inherited; remove app resources, user_api_scopes and permissions declarations.")
    for target in doc.get("targets", {}).values():
        if target.get("permissions") or target.get("presets"):
            raise click.ClickException("App Space deployment does not support target permission or preset overrides.")
        override = target.get("resources", {}).get("apps", {}).get(bundle_key, {})
        if set(override) - {"name"}:
            raise click.ClickException("App Space runtime configuration must be in the root app declaration, not target overrides.")
        if any(name.endswith("_keepalive") for name in target.get("resources", {}).get("jobs", {})):
            raise click.ClickException("Configure the paused keepalive job in the root declaration, not target overrides.")
    env = app.get("config", {}).get("env", [])
    hosts = [entry.get("value") for entry in env if entry.get("name") == "APX_APPS_HOST"]
    if hosts != ["agentbricks"]:
        raise click.ClickException("This App Space deployment path requires APX_APPS_HOST=agentbricks.")
    validate_space_env(env)
    for name, job in doc.get("resources", {}).get("jobs", {}).items():
        if name.endswith("_keepalive") and job.get("schedule", {}).get("pause_status") != "PAUSED":
            raise click.ClickException("Pause or remove the generated keepalive job before deploying to an App Space.")
    return space


def validate_space_deployment(
    doc: dict[str, Any], *, bundle_key: str, app_name: str, profile: str | None,
    plan: Any, family_permissions: Any,
) -> Any:
    """Read inherited policy; never migrate an app or widen a space's grants."""
    space = validate_space_bundle(doc, bundle_key=bundle_key)
    if not profile:
        raise click.ClickException("App Space deployment requires an explicit --profile.")
    if family_permissions.can_use_groups or family_permissions.can_manage_groups:
        raise click.ClickException("App Space deployment cannot reconcile app-family grants; configure access through the space.")

    try:
        from databricks_agentkit._api_client import _AgentBricksApiClient
    except ImportError as exc:
        raise click.ClickException("Install apx-agent[agentbricks] to deploy the durable target.") from exc
    from databricks.sdk.errors import NotFound
    from ._resources import resources_to_databricks_yml

    client = _AgentBricksApiClient(profile)
    inherited = client.workspace_client.apps.get_space(space)
    missing = set(plan.user_api_scopes) - set(inherited.effective_user_api_scopes or [])
    if missing:
        raise click.ClickException("App Space is missing required scopes: " + ", ".join(sorted(missing)))
    resources = [resource.as_dict() for resource in inherited.resources or []]
    for required in resources_to_databricks_yml(plan.service_resources):
        if not any(all(entry.get(key) == value for key, value in required.items() if key != "name") for entry in resources):
            raise click.ClickException("App Space lacks a required service resource; configure it in the space before deployment.")
    try:
        existing = client.workspace_client.apps.get(app_name)
    except NotFound:
        existing = None
    if existing is not None and existing.space != space:
        raise click.ClickException("Refusing to move an existing app into a different App Space.")
    return client


def runtime_store_env(client: Any, *, app_name: str, space: str) -> dict[str, str]:
    """Bind only a Runtime Store whose app and principal ownership the SDK verifies."""
    from databricks_agentbricks.lakebase_runtime_store import get_or_create_backend
    from databricks_agentkit.runtime.store import (
        RUNTIME_STORE_LAKEBASE_BRANCH_ENV, RUNTIME_STORE_DATABASE_ENV,
        RUNTIME_STORE_USERNAME_ENV, RUNTIME_STORE_SCHEMA_ENV, DEFAULT_RUNTIME_STORE_SCHEMA,
    )

    from urllib.parse import quote

    # The installed SDK enum predates LIQUID and deserializes it as None.
    app = client.workspace_client.api_client.do("GET", f"/api/2.0/apps/{quote(app_name, safe='')}")
    if app.get("space") != space or app.get("compute_size") != "LIQUID":
        raise click.ClickException("App readback did not confirm the declared space and LIQUID compute.")
    backend = get_or_create_backend(client, app_name, app.get("service_principal_client_id"))
    return {
        RUNTIME_STORE_LAKEBASE_BRANCH_ENV: backend.branch,
        RUNTIME_STORE_DATABASE_ENV: backend.database_id,
        RUNTIME_STORE_USERNAME_ENV: backend.username,
        RUNTIME_STORE_SCHEMA_ENV: DEFAULT_RUNTIME_STORE_SCHEMA,
    }
