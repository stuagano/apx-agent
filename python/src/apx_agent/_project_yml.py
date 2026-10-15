"""Databricks asset-bundle YAML emitters.

``_build_databricks_yml`` and ``_build_app_yml`` are pure functions of the
config. Split out of ``_project_gen`` so the bundle shape can change without
touching the project-directory writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._models import AgentConfig


def _build_databricks_yml(config: "AgentConfig") -> str:
    """Build databricks.yml bundle definition for *config*.

    Includes a ``cp -r skills .build/`` step when the config declares skills so
    that the skill markdown files land in the deployed app's source root.

    :param config: Validated ``AgentConfig`` describing the agent.
    :returns: Complete ``databricks.yml`` content as a string.
    """
    from ._memory_wiring import (  # noqa: PLC0415
        declared_max_replicas,
        declared_replica_bounds,
        scaled_in_memory,
        scaled_in_memory_error,
    )

    # Compile-time guard (FR-3): refuse to emit a bundle for a scaled app whose
    # session state is in-memory. ws=None here — session_is_in_memory treats a
    # declared lakebase session as durable-intent, so scaled+lakebase passes.
    if scaled_in_memory(config, ws=None):
        raise ValueError(scaled_in_memory_error(declared_max_replicas(config)))

    name = config.name
    skills_copy = (
        "      cp -r skills .build/ 2>/dev/null || true\n\n" if config.skills else "\n"
    )
    # Carry the declared replica count forward for the runtime boot guard (FR-4).
    # The bundle now also carries native Apps horizontal-scaling bounds, but the
    # runtime still needs an explicit read-back because the process cannot discover
    # its deployed replica count from the Apps environment.
    declared_instances_env = (
        f"""
          - name: APX_DECLARED_INSTANCES
            value: "{declared_max_replicas(config)}\""""
        if config.deploy is not None
        else ""
    )
    replica_bounds = declared_replica_bounds(config)
    scaling_fields = (
        f"      compute_min_instances: {replica_bounds.min}\n"
        f"      compute_max_instances: {replica_bounds.max}\n"
        if replica_bounds is not None
        else ""
    )
    document = f"""\
bundle:
  name: {name}

# The root apx-agent ``.gitignore`` excludes ``**/.build/`` because that
# directory is built from source by ``databricks bundle deploy``. DAB sync
# honours ``.gitignore`` when uploading, so ``sync.include`` overrides the
# ignore for the staged tree only.
sync:
  include:
    - .build/**

variables:
  workspace_user:
    description: User who owns the experiment
    default: ${{workspace.current_user.userName}}
  llm_endpoint_name:
    description: Foundation model endpoint the agent calls.
    default: {config.model}
  mlflow_experiment_id:
    description: |
      MLflow experiment ID for tracing. Populate via scripts/quickstart.py
      into a local .env file; surfaced here for the app environment.
    default: ""

artifacts:
  default:
    build: |
      mkdir -p .build
      cp agent.py .build/ 2>/dev/null || true
      cp tools.py .build/ 2>/dev/null || true
      cp -r sub_agents .build/ 2>/dev/null || true
      cp -r agent_server scripts README.md .build/ 2>/dev/null || true
      cp -r .apx-agent .build/ 2>/dev/null || true
      cp -r .apx .build/ 2>/dev/null || true
      cp apx_agent-*.whl .build/ 2>/dev/null || true
{skills_copy}
resources:
  experiments:
    {name}_experiment:
      name: /Users/${{var.workspace_user}}/${{bundle.name}}-${{bundle.target}}

  apps:
    {name}:
      name: {name}
      description: {name} apx-agent
      source_code_path: ./.build
{scaling_fields}      user_api_scopes:
        - ai-gateway
        - sql
      resources:
        - name: experiment
          experiment:
            experiment_id: ${{resources.experiments.{name}_experiment.id}}
            permission: CAN_MANAGE
      config:
        command:
          - python
          - -m
          - agent_server.start_host
        env:
          - name: APX_MODEL
            value: ${{var.llm_endpoint_name}}
          - name: MLFLOW_TRACKING_URI
            value: databricks
          - name: MLFLOW_EXPERIMENT_ID
            value: ${{var.mlflow_experiment_id}}
          - name: APX_AGENT_MLFLOW_AUTOLOG
            value: "1"
          - name: APX_RUNTIME
            value: apps{declared_instances_env}

  jobs:
    {name}_keepalive:
      name: {name}-keepalive
      description: Pings /readyz every 5 minutes to prevent cold starts.
      schedule:
        quartz_cron_expression: "0 0/5 * * * ?"
        timezone_id: UTC
        pause_status: UNPAUSED
      environments:
        - environment_key: default
          spec:
            client: "1"
            dependencies:
              - requests
      tasks:
        - task_key: ping
          environment_key: default
          spark_python_task:
            python_file: ./.build/agent_server/keepalive.py
            parameters:
              - ${{resources.apps.{name}.url}}/readyz

targets:
  dev:
    mode: development
    default: true
  prod:
    mode: production
    resources:
      apps:
        {name}:
          name: {name}
"""
    if config.target == "responses_agent":
        return document

    import yaml
    from ._app_space import RUNTIME_STORE_VARIABLES

    bundle = yaml.safe_load(document)
    app = bundle["resources"]["apps"][name]
    env = app["config"]["env"]
    # Native deployment delegates tracing and rollout to Agent Bricks.
    for key in ("experiments", "jobs"):
        bundle["resources"].pop(key, None)
    app.pop("resources", None)
    app.pop("user_api_scopes", None)
    for key in ("workspace_user", "mlflow_experiment_id"):
        bundle["variables"].pop(key, None)
    app["config"]["env"] = [
        entry for entry in env if entry["name"] != "MLFLOW_EXPERIMENT_ID"
    ]
    env = app["config"]["env"]
    if config.deploy is None or config.deploy.space is None:
        app.pop("description", None)
        deployment_name = (
            name if name.startswith("agent-bricks-") else f"agent-bricks-{name}"
        )
        app["name"] = deployment_name
        bundle["targets"]["prod"]["resources"]["apps"][name]["name"] = deployment_name
    if config.deploy is not None and config.deploy.space is not None:
        app["space"] = config.deploy.space
        app["config"]["env"] = [
            entry for entry in env if entry["name"] != "APX_DECLARED_INSTANCES"
        ]
        for key, variable in RUNTIME_STORE_VARIABLES.items():
            bundle["variables"][variable] = {
                "description": "SDK-owned Runtime Store coordinate, bound by apx-agent deploy",
                "default": "",
            }
            app["config"]["env"].append(
                {"name": key, "value": "${var." + variable + "}"}
            )
    return yaml.safe_dump(bundle, sort_keys=False)


def _build_app_yml(config: "AgentConfig") -> str:
    """Build app.yml compute config for *config*."""
    return """\
command:
  - python
  - -m
  - agent_server.start_host
env:
  - name: MLFLOW_TRACKING_URI
    value: databricks
  - name: APX_AGENT_MLFLOW_AUTOLOG
    value: "1"
"""


