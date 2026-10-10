"""Apps API durable deploy: seven get-or-create steps, no Agent Bricks CLI."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from ._apps_authorization import AppFamilyPermissions, AuthorizationPlan

GRANT_UPDATE_MASK = "user_api_scopes,resources"
APX_MODEL_ENV = "APX_MODEL"
TRACKING_ENV = "MLFLOW_TRACKING_URI"
EXPERIMENT_ENV = "MLFLOW_EXPERIMENT_ID"
MEMORY_ENV = "AGENT_MEMORY_STORE"
SESSION_ENV = "AGENT_SESSION_STORE"


def deploy_apps_api(
    *,
    cwd: Path,
    agent: Any,
    config: Any,
    plan: AuthorizationPlan,
    family_permissions: AppFamilyPermissions,
    workspace: Any,
    profile: str | None,
    bundle_target: str,
    app_name: str,
    auto_experiment: bool,
    auto_build_wheel: bool,
    readyz_gate: bool,
    register_uc: bool,
    uc_name: str | None,
    module: str,
    no_run: bool,
    vars: tuple[str, ...],
    env_pairs: tuple[str, ...],
    secret_env_pairs: tuple[str, ...],
    app_name_override: str | None,
    json_output: bool,
    extra_version_tags: dict[str, str] | None,
    poll_timeout_seconds: int,
    readyz_attempts: int,
    pin: Any,
    log: Callable[[str], None],
) -> str:
    """Run the Apps API path. Re-running is safe; nothing is deleted on failure."""
    if config.target != "durable_agent_server" or config.deploy is None or config.deploy.backend != "apps_api":
        raise click.ClickException("Apps API deploy requires target='durable_agent_server' and backend='apps_api'.")
    if config.deploy.entrypoint is None:
        raise click.ClickException("backend='apps_api' requires deploy.entrypoint.")
    if no_run:
        raise click.ClickException("Apps API deploy includes rollout; --no-run is unsupported.")
    if not profile:
        raise click.ClickException("Apps API deployment requires an explicit --profile.")
    if vars or secret_env_pairs:
        raise click.ClickException("Apps API projects do not use Bundle --var or --secret-env; declare deploy.env or use --env.")
    if app_name_override and app_name_override != app_name:
        raise click.ClickException("Apps API uses the declared app name; --app-name cannot rename it.")
    if family_permissions.can_use_groups or family_permissions.can_manage_groups:
        raise click.ClickException("Apps API deployment cannot reconcile app-family grants; configure access separately.")

    from ._runtime_targets import inspect_target

    report = inspect_target(agent, config=config, target="durable_agent_server")
    declared_memory = config.memory is not None
    if not declared_memory or any(name != "long_term_memory" for name in report.unsatisfied):
        report.require_compatible()
    space = config.deploy.space
    log(f"# Apps API deploy {app_name}" + (f" (space {space})" if space else " (dedicated)"))

    build_dir = _stage(cwd, config, agent, plan, log)
    _ensure_stores(workspace, config, report.session_store, log)
    app = _ensure_app(workspace, app_name=app_name, space=space, description=config.description or None, log=log)
    _apply_grants(workspace, app_name=app_name, space=space, profile=profile, plan=plan, family_permissions=family_permissions, log=log)
    env = _runtime_and_env(workspace, profile=profile, app_name=app_name, space=space, config=config,
                           session_store=report.session_store, bundle_target=bundle_target, auto_experiment=auto_experiment,
                           env_pairs=env_pairs, app=app, log=log)
    _write_app_yaml(build_dir, config.deploy.entrypoint, env)
    _stage_lock(build_dir, auto_build_wheel, log)
    _upload_and_rollout(build_dir, app_name=app_name, profile=profile, workspace=workspace, log=log)
    return _verify(
        cwd=cwd, app_name=app_name, profile=profile, module=module, bundle_target=bundle_target,
        readyz_gate=readyz_gate, readyz_attempts=readyz_attempts, poll_timeout_seconds=poll_timeout_seconds,
        register_uc=register_uc, uc_name=uc_name, session_store=report.session_store,
        agent_name=_agent_name(agent), experiment_id=env.get(EXPERIMENT_ENV),
        extra_version_tags=extra_version_tags, pin=pin, json_output=json_output, log=log,
    )


def preflight_apps_api(cwd: Path, config: Any, agent: Any) -> str:
    """Offline declaration and staged-source check for doctor rows 3 and 4."""
    deploy = config.deploy
    if deploy is None or deploy.backend != "apps_api" or deploy.entrypoint is None:
        raise click.ClickException("Apps API doctor preflight requires backend='apps_api' and deploy.entrypoint.")
    from ._agents import LlmAgent
    from ._runtime_targets import inspect_target

    report = inspect_target(agent, config=config, target="durable_agent_server")
    declared_memory = config.memory is not None
    if not declared_memory or any(name != "long_term_memory" for name in report.unsatisfied):
        report.require_compatible()
    manifest = cwd / ".build" / "agent.toml"
    if manifest.is_file() and not isinstance(agent, LlmAgent) and "[session_store]" in manifest.read_text():
        raise click.ClickException("Staged agent.toml declares a session_store a composite agent cannot bind.")
    staged = cwd / ".build" / "app.yaml"
    if staged.is_file():
        import yaml

        doc = yaml.safe_load(staged.read_text()) or {}
        if doc.get("command") != ["python", "-m", deploy.entrypoint]:
            raise click.ClickException("Staged app.yaml command disagrees with deploy.entrypoint.")
    placement = f"space {deploy.space}" if deploy.space else "dedicated"
    session = "session preflight required" if report.session_store else "no managed session"
    return f"Apps API {placement} preflight passes for {config.name} ({session}; no workspace objects created)"


def _stage(cwd: Path, config: Any, agent: Any, plan: AuthorizationPlan, log: Callable[[str], None]) -> Path:
    from ._agentbricks_deploy import stage_native_source
    from ._durable_agent import build_native_manifest

    for filename in ("agent.toml", "app.yaml", "app.yml"):
        if (cwd / filename).exists() or (cwd / filename).is_symlink():
            raise click.ClickException(f"Apps API deployment generates {filename} in .build; refusing to ignore an existing root manifest.")
    stage_native_source(cwd, include=tuple(config.deploy.include))
    source = cwd / ".build"
    if not source.is_dir() or not source.resolve().is_relative_to(cwd.resolve()):
        raise click.ClickException("The Apps API path requires a staged source directory inside the project.")
    manifest = source / "agent.toml"
    if manifest.exists() and not manifest.read_text().startswith("# Generated by APX"):
        raise click.ClickException("Refusing to overwrite an authored agent.toml in the staged source directory.")
    manifest.write_text(build_native_manifest(config=config, authorization_plan=plan, agent=agent))
    log("  staged source and compiled agent.toml")
    return source


def _ensure_stores(workspace: Any, config: Any, session_store: str | None, log: Callable[[str], None]) -> None:
    if config.memory is not None and config.memory.type == "managed":
        from ._memory_managed import provision_managed_memory

        try:
            log("  " + provision_managed_memory(workspace, config.memory.store_name).splitlines()[0])
        except Exception as exc:
            raise click.ClickException(
                f"Could not ensure memory store {config.memory.store_name!r}: {exc}. "
                "A permission error is not retried as a create; grant access and rerun."
            ) from exc
    if session_store is None:
        return
    from databricks_agentkit import AgentKitClient

    try:
        AgentKitClient(workspace_client=workspace).session_stores.get(session_store)
    except Exception as exc:
        raise click.ClickException(
            f"Could not read session store {session_store!r}: {exc}. "
            "Apps API deploy does not create session stores; create it and grant read access, then rerun."
        ) from exc
    log(f"  session store {session_store} exists")


def _ensure_app(workspace: Any, *, app_name: str, space: str | None, description: str | None, log: Callable[[str], None]) -> Any:
    from databricks.sdk.errors import NotFound
    from databricks.sdk.service.apps import App

    try:
        existing = workspace.apps.get(app_name)
    except NotFound:
        existing = None
    except Exception as exc:
        raise click.ClickException(f"Could not read app {app_name!r}: {exc}. Check workspace access and retry.") from exc
    if existing is not None:
        _refuse_move(existing, space)
        log(f"  reusing app {app_name}")
        _wait_compute_active(workspace, app_name, log)
        return workspace.apps.get(app_name)
    if space is not None:
        _require_space(workspace, space)
    created = App(name=app_name, description=description, **({"space": space} if space is not None else {}))
    try:
        app = workspace.apps.create(created).result()
    except Exception as exc:
        raise click.ClickException(
            f"Could not create app {app_name!r}" + (f" in space {space!r}" if space else "")
            + f": {exc}. Check Apps permissions and retry; nothing was deleted."
        ) from exc
    log(f"  created app {app_name}")
    _wait_compute_active(workspace, app_name, log)
    # Compute ACTIVE is when the service principal exists. Grants and the
    # Runtime Store both need that identity, not the create response.
    return workspace.apps.get(app_name)


def _app_space(existing: Any) -> str | None:
    current = existing.space
    return current if isinstance(current, str) and current else None


def _agent_name(agent: Any) -> str | None:
    name = vars(agent).get("_name")
    return name if isinstance(name, str) and name else None


def _refuse_move(existing: Any, space: str | None) -> None:
    current = _app_space(existing)
    if current == space:
        return
    if space is None:
        raise click.ClickException("Refusing to turn a space app into a dedicated app.")
    if current is None:
        raise click.ClickException("Refusing to move an existing dedicated app into an App Space.")
    raise click.ClickException("Refusing to move an existing app into a different App Space.")


def _require_space(workspace: Any, space: str) -> None:
    from databricks.sdk.errors import NotFound

    try:
        workspace.apps.get_space(space)
    except NotFound as exc:
        raise click.ClickException(f"App Space {space!r} was not found. Create it before deploying; this path does not create spaces.") from exc
    except Exception as exc:
        raise click.ClickException(f"Could not read App Space {space!r}: {exc}.") from exc


def _compute_state(status: Any) -> str:
    """SDK states are ComputeState enums; tests and older payloads are strings."""
    raw_state = getattr(status, "state", None) if status is not None else None
    value = getattr(raw_state, "value", raw_state)
    return value.upper() if isinstance(value, str) else ""


def _wait_compute_active(workspace: Any, app_name: str, log: Callable[[str], None], *, timeout_seconds: int = 300) -> None:
    deadline = time.time() + timeout_seconds
    delay = 1.0
    started = False
    while True:
        app = workspace.apps.get(app_name)
        state = _compute_state(app.compute_status)
        if state == "ACTIVE":
            return
        if state == "ERROR":
            raise click.ClickException(f"App {app_name!r} compute entered ERROR. Nothing was deleted; inspect the app and retry.")
        if state == "STOPPED" and not started:
            # A scaled-to-zero space app stays STOPPED until start. One call;
            # later polls wait for ACTIVE rather than starting again.
            try:
                workspace.apps.start(app_name)
            except Exception as exc:
                raise click.ClickException(
                    f"Could not start app {app_name!r}: {exc}. Nothing was deleted; check Apps permissions and retry."
                ) from exc
            started = True
            log(f"  compute=STOPPED; started {app_name}")
        elif time.time() >= deadline:
            shown = state if state else "?"
            raise click.ClickException(f"Timed out waiting for app {app_name!r} compute to become ACTIVE (last state {shown}).")
        else:
            shown = state if state else "?"
            log(f"  compute={shown}; waiting")
        time.sleep(min(delay, max(0.0, deadline - time.time())))
        delay = min(delay * 1.5, 15.0)


def _apply_grants(
    workspace: Any, *, app_name: str, space: str | None, profile: str | None,
    plan: AuthorizationPlan, family_permissions: AppFamilyPermissions, log: Callable[[str], None],
) -> None:
    if space is not None:
        from ._app_space import validate_space_deployment

        # The existing check reads scopes and resources and refuses a space move.
        # It does not update the app. A synthetic bundle keeps that contract intact.
        validate_space_deployment(
            _space_check_doc(app_name, space), bundle_key=app_name, app_name=app_name, profile=profile,
            plan=plan, family_permissions=family_permissions,
        )
        log("# App Space: inherited authorization checked; no app grant update sent")
        return
    from databricks.sdk.service.apps import App, AppResource
    from ._resources import resources_to_databricks_yml

    resources = [AppResource.from_dict(entry) for entry in resources_to_databricks_yml(plan.service_resources)]
    # The SDK requires name. The mask is what the API applies; name is not in it.
    # Empty lists are the declared sets. Omitting them could leave old grants in place.
    app = App(name=app_name, user_api_scopes=list(plan.user_api_scopes), resources=resources)
    try:
        workspace.apps.create_update(app_name, GRANT_UPDATE_MASK, app=app).result()
    except Exception as exc:
        raise click.ClickException(
            f"Could not update grants on dedicated app {app_name!r}: {exc}. "
            "The update mask is user_api_scopes,resources only; grant Apps update permission and retry."
        ) from exc
    log("  updated dedicated app grants")


def _space_check_doc(app_name: str, space: str) -> dict[str, Any]:
    return {"resources": {"apps": {app_name: {
        "name": app_name, "space": space, "source_code_path": ".build",
        "config": {"command": ["python", "-m", "app"], "env": [{"name": "APX_APPS_HOST", "value": "agentbricks"}]},
    }}}}


def _runtime_and_env(
    workspace: Any, *, profile: str | None, app_name: str, space: str | None, config: Any,
    session_store: str | None, bundle_target: str, auto_experiment: bool, env_pairs: tuple[str, ...],
    app: Any, log: Callable[[str], None],
) -> dict[str, str]:
    from databricks_agentkit._api_client import _AgentBricksApiClient
    from ._app_space import runtime_store_env

    client = _AgentBricksApiClient(profile) if profile else _AgentBricksApiClient(workspace_client=workspace)
    principal = app.service_principal_client_id
    runtime = runtime_store_env(
        client, app_name=app_name, space=space,
        service_principal_id=principal if isinstance(principal, str) else None,
    )
    env = {
        APX_MODEL_ENV: config.model,
        TRACKING_ENV: "databricks",
        **runtime,
        **dict(config.deploy.env),
    }
    for raw in env_pairs:
        from .cli import _parse_env_flag

        pair = _parse_env_flag(raw)
        if pair.name in env:
            raise click.ClickException(f"--env conflicts with declared or generated {pair.name}; existing settings are preserved.")
        env[pair.name] = pair.value
    if config.memory is not None and config.memory.type == "managed":
        env[MEMORY_ENV] = config.memory.store_name
    if session_store is not None:
        env[SESSION_ENV] = session_store
    experiment_id = _resolve_experiment(profile, app_name, bundle_target, auto_experiment, log)
    if experiment_id:
        env[EXPERIMENT_ENV] = experiment_id
        principal = app.service_principal_client_id
        if isinstance(principal, str) and principal:
            from .cli import _grant_experiment_to_sp, _grant_trace_uc_tables_to_sp

            if _grant_experiment_to_sp(experiment_id, principal, profile=profile):
                log("  granted app SP CAN_MANAGE on tracing experiment")
            if _grant_trace_uc_tables_to_sp(experiment_id, principal, profile=profile):
                log("  granted app SP UC permissions on trace tables")
    return env


def _resolve_experiment(profile: str | None, app_name: str, bundle_target: str, auto_experiment: bool, log: Callable[[str], None]) -> str | None:
    if not auto_experiment:
        return None
    from .cli import _ensure_experiment_id

    return _ensure_experiment_id(profile, app_name, bundle_target, None)


def _write_app_yaml(build_dir: Path, entrypoint: str, env: dict[str, str]) -> None:
    import yaml

    path = build_dir / "app.yaml"
    if path.exists() and not path.read_text().startswith("# Generated by APX"):
        raise click.ClickException("Refusing to overwrite authored app.yaml in the staged source.")
    document = {
        "command": ["python", "-m", entrypoint],
        "env": [{"name": name, "value": value} for name, value in env.items()],
    }
    path.write_text("# Generated by APX from agent declarations; do not edit.\n" + yaml.safe_dump(document, sort_keys=False))


def _stage_lock(build_dir: Path, auto_build_wheel: bool, log: Callable[[str], None]) -> None:
    from .cli import _ensure_apx_wheel, _stage_build_manifest

    wheel = _ensure_apx_wheel(build_dir.parent) if auto_build_wheel else None
    _stage_build_manifest(build_dir, wheel)
    if not (build_dir / "pyproject.toml").exists():
        raise click.ClickException("deploy aborted: .build/pyproject.toml is missing after staging.")
    log("  staged dependency manifest")


def _upload_and_rollout(build_dir: Path, *, app_name: str, profile: str, workspace: Any, log: Callable[[str], None]) -> None:
    from .cli import _run_databricks_cmd

    me = workspace.current_user.me()
    user = me.user_name
    if not isinstance(user, str) or not user or "/" in user:
        raise click.ClickException("Could not resolve the current workspace user for the Apps API source path.")
    destination = f"/Workspace/Users/{user}/apx_deployments/{app_name}"
    sync = _run_databricks_cmd(["sync", str(build_dir), destination, "--exclude", "uv.lock"], profile)
    if sync.returncode != 0:
        raise click.ClickException(f"`databricks sync` failed (exit {sync.returncode}); source was not replaced and nothing was deleted.")
    deploy = _run_databricks_cmd(["apps", "deploy", app_name, "--source-code-path", destination], profile)
    if deploy.returncode != 0:
        raise click.ClickException(f"`databricks apps deploy` failed (exit {deploy.returncode}); the previous deployment was left in place.")
    log(f"  uploaded {destination} and rolled out {app_name}")


def _verify(
    *, cwd: Path, app_name: str, profile: str | None, module: str, bundle_target: str,
    readyz_gate: bool, readyz_attempts: int, poll_timeout_seconds: int, register_uc: bool,
    uc_name: str | None, session_store: str | None, agent_name: str | None, experiment_id: str | None,
    extra_version_tags: dict[str, str] | None, pin: Any, json_output: bool, log: Callable[[str], None],
) -> str:
    import json

    from .cli import (
        _check_native_execution, _check_readyz, _fetch_app_log_tail, _maybe_write_deploy_state,
        _poll_app_ready, _read_apx_agent_config, _register_apps_manifest_step, _resolve_apps_uc_name,
    )

    payload = _poll_app_ready(app_name, profile, timeout_seconds=poll_timeout_seconds, log=log)
    raw_url = payload.get("url")
    if not isinstance(raw_url, str) or not raw_url:
        raise click.ClickException(f"App {app_name!r} is RUNNING but has no URL; verification stopped and nothing was deleted.")
    app_url = raw_url
    checks: dict[str, Any] | None = None
    if readyz_gate:
        log(f"# readyz gate: GET {app_url}/readyz")
        ok, checks = _check_readyz(app_url, profile=profile, attempts=readyz_attempts)
        ok = ok and isinstance(checks, dict) and checks.get("durable") is True
        if ok:
            log(f"# execution smoke: POST {app_url}/api/invocations")
            smoke = _check_native_execution(app_url, profile=profile, session_store=session_store, agent_name=agent_name)
            ok = smoke.completed
            if not ok and isinstance(checks, dict):
                checks = {**checks, "agent_execution": smoke.detail}
        if not ok:
            if register_uc:
                _register_apps_manifest_step(
                    module=module, config=_read_apx_agent_config(), app_name=app_name, bundle_target=bundle_target,
                    uc_name_override=uc_name, extra_version_tags={**(extra_version_tags or {}), "apx.apps.readyz": "failed"},
                    experiment_id=experiment_id, profile=profile, load_cwd=cwd / ".build", required=False, log=log,
                )
            raise click.ClickException(
                f"readyz gate failed — the app is STILL LIVE:\n  app: {app_name}\n  url: {app_url}\n{checks}\n"
                f"{_fetch_app_log_tail(app_name, profile=profile)}\nNothing was deleted."
            )
        log(f"  readyz: ready ({checks})")
    registered_version = None
    if register_uc:
        registered_version = _register_apps_manifest_step(
            module=module, config=_read_apx_agent_config(), app_name=app_name, bundle_target=bundle_target,
            uc_name_override=uc_name, extra_version_tags=extra_version_tags, experiment_id=experiment_id,
            profile=profile, load_cwd=cwd / ".build", required=True, log=log,
        )
    recorded = _maybe_write_deploy_state(
        profile=profile, app_name=app_name, bundle_target=bundle_target, app_url=app_url,
        experiment_id=experiment_id, pin=pin, wheel_path=None, log=log,
    )
    if json_output:
        click.echo(json.dumps({
            "ok": True, "app_name": app_name, "app_url": app_url, "bundle_target": bundle_target,
            "uc_name": _resolve_apps_uc_name(_read_apx_agent_config(), app_name, override=uc_name),
            "version": registered_version, "readyz": checks, "deploy_state_recorded": recorded,
            "deployment_backend": "apps_api",
        }, default=str))
    else:
        log(f"# app ready: {app_name}")
        click.echo(app_url)
    return app_url
