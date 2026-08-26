"""Bootstrap helpers for apx-agent Apps deployments.

This module hosts the small amount of pre-deploy plumbing that every
``apx-agent scaffold --target apps`` project needs: creating an MLflow experiment
at the canonical workspace path, binding UC-backed trace storage when the
scaffold has a catalog/schema, and writing the experiment id into a local
``.env`` so the bundle and the agent module pick it up.

The scaffolded ``scripts/quickstart.py`` is a thin wrapper around
:func:`init_apps_experiment` — the logic lives here so we don't ship the
same 80-line script into every example.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, NamedTuple

_DEFAULT_BUNDLE_TARGET = "dev"  # fall back to local dev mode when BUNDLE_TARGET not set
_DEFAULT_MLFLOW_TRACKING_URI = "databricks"  # MLflow default for Databricks workspaces


class ExperimentInfo(NamedTuple):
    experiment_path: str
    experiment_id: str


def _resolve_user(user: str | None) -> str:
    """Best-effort user identity for the experiment path.

    Honors an explicit ``user`` first, then falls back through the standard
    env vars (``DATABRICKS_USER`` → ``USER`` → ``USERNAME``) before landing
    on ``"unknown-user"``. Matches the data-triage / memory_demo example
    quickstart behavior — no ``databricks current-user me`` subprocess.
    """
    if user:
        return user
    for env_var in ("DATABRICKS_USER", "USER", "USERNAME"):
        value = os.environ.get(env_var)
        if value:
            return value
    return "unknown-user"


def _resolve_target(target: str | None) -> str:
    """Bundle target — defaults to ``BUNDLE_TARGET`` env, then ``"dev"``."""
    if target:
        return target
    return os.environ.get("BUNDLE_TARGET", _DEFAULT_BUNDLE_TARGET)


def _resolve_agent_name(agent_name: str | None) -> str:
    """Agent name — defaults to the cwd directory name."""
    if agent_name:
        return agent_name
    return Path.cwd().name


def _resolve_tracking_uri(tracking_uri: str | None) -> str:
    """Tracking URI — defaults to ``MLFLOW_TRACKING_URI`` env, then ``"databricks"``."""
    if tracking_uri:
        return tracking_uri
    return os.environ.get("MLFLOW_TRACKING_URI", _DEFAULT_MLFLOW_TRACKING_URI)


def _ensure_experiment(
    experiment_path: str,
    tracking_uri: str,
    *,
    catalog_name: str | None = None,
    schema_name: str | None = None,
    table_prefix: str | None = None,
) -> str:
    """Create or reuse the MLflow experiment at ``experiment_path``.

    Returns the experiment id. The ``mlflow`` import is deferred so this
    module stays cheap to import in environments without mlflow installed
    (tests, doc tooling).
    """
    try:
        import mlflow.tracking
    except ImportError as exc:
        raise SystemExit("mlflow is required. Install with: uv sync") from exc

    mlflow.set_tracking_uri(tracking_uri)

    if catalog_name and schema_name:
        try:
            from mlflow.entities.trace_location import UnityCatalog
        except ImportError as exc:
            raise SystemExit(
                "Unity Catalog trace storage requires mlflow[databricks]>=3.14. "
                "Run: uv sync"
            ) from exc
        unity_catalog: Any = UnityCatalog
        experiment = mlflow.set_experiment(
            experiment_name=experiment_path,
            trace_location=unity_catalog(
                catalog_name=catalog_name,
                schema_name=schema_name,
                table_prefix=table_prefix,
            ),
        )
        exp_id = str(experiment.experiment_id)
        print(
            f"  bind   experiment {experiment_path} "
            f"to UC traces {catalog_name}.{schema_name}.{table_prefix or exp_id}"
        )
        return exp_id

    client = mlflow.tracking.MlflowClient()
    existing = client.get_experiment_by_name(experiment_path)
    if existing is not None:
        print(f"  reuse  experiment {experiment_path} (id={existing.experiment_id})")
        return existing.experiment_id

    exp_id = client.create_experiment(experiment_path)
    print(f"  create experiment {experiment_path} (id={exp_id})")
    return exp_id


def _write_experiment_id(env_path: Path, experiment_id: str) -> None:
    """Write ``MLFLOW_EXPERIMENT_ID=<id>`` to ``env_path``.

    Preserves every other line that's already in the file: any pre-existing
    ``MLFLOW_EXPERIMENT_ID=...`` line is replaced (preventing duplicates on
    re-run), and a new line is appended if no such line exists. If the file
    doesn't exist yet, it's created with just the experiment-id line.
    """
    new_line = f"MLFLOW_EXPERIMENT_ID={experiment_id}"

    if not env_path.exists():
        env_path.write_text(new_line + "\n")
        print(f"  write  {env_path}")
        return

    existing = env_path.read_text().splitlines()
    kept = [line for line in existing if not line.startswith("MLFLOW_EXPERIMENT_ID=")]
    kept.append(new_line)
    env_path.write_text("\n".join(kept) + "\n")
    print(f"  write  {env_path}")


def init_apps_experiment(
    *,
    user: str | None = None,
    target: str | None = None,
    agent_name: str | None = None,
    tracking_uri: str | None = None,
    env_path: str | None = None,
    catalog_name: str | None = None,
    schema_name: str | None = None,
    table_prefix: str | None = None,
) -> ExperimentInfo:
    """Create the MLflow experiment for an Apps-target deploy and persist its id.

    Resolves identity and target with the same default order the example
    quickstart scripts use:

    - ``user``: explicit arg → ``$DATABRICKS_USER`` → ``$USER`` →
      ``$USERNAME`` → ``"unknown-user"``.
    - ``target``: explicit arg → ``$BUNDLE_TARGET`` → ``"dev"``.
    - ``agent_name``: explicit arg → name of the current working directory.
    - ``tracking_uri``: explicit arg → ``$MLFLOW_TRACKING_URI`` →
      ``"databricks"``.
    - ``env_path``: explicit arg → ``.env`` in the current working directory.

    Creates (or reuses) ``/Users/<user>/<agent_name>-<target>`` as the
    MLflow experiment, then writes ``MLFLOW_EXPERIMENT_ID=<id>`` to
    ``env_path``. Existing lines in ``env_path`` are preserved; only the
    ``MLFLOW_EXPERIMENT_ID`` line is replaced. Safe to re-run.

    Returns
    -------
    tuple[str, str]
        ``(experiment_path, experiment_id)``.
    """
    resolved_user = _resolve_user(user)
    resolved_target = _resolve_target(target)
    resolved_agent = _resolve_agent_name(agent_name)
    resolved_tracking = _resolve_tracking_uri(tracking_uri)

    experiment_path = f"/Users/{resolved_user}/{resolved_agent}-{resolved_target}"
    experiment_id = _ensure_experiment(
        experiment_path,
        resolved_tracking,
        catalog_name=catalog_name,
        schema_name=schema_name,
        table_prefix=table_prefix,
    )

    resolved_env_path = Path(env_path) if env_path else Path.cwd() / ".env"
    _write_experiment_id(resolved_env_path, experiment_id)

    return ExperimentInfo(experiment_path=experiment_path, experiment_id=experiment_id)


def _sql_statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";") if statement.strip()]


def provision_lakehouse_observability(
    *,
    catalog_name: str | None = None,
    schema_name: str | None = None,
    table_prefix: str | None = None,
    sql_path: str | None = None,
    warehouse_id: str | None = None,
) -> list[str]:
    """Apply the generated APX observability SQL when a warehouse is configured."""
    if not (catalog_name and schema_name):
        return []

    path = Path(sql_path) if sql_path else Path.cwd() / ".apx" / "sql" / "apx_agent_timeline.sql"
    if not path.is_file():
        return [f"  skip   observability SQL — {path} not found"]

    wh_id = warehouse_id or os.environ.get("MLFLOW_TRACING_SQL_WAREHOUSE_ID")
    if not wh_id:
        return [
            "  skip   observability SQL — set MLFLOW_TRACING_SQL_WAREHOUSE_ID "
            "to create apx_agent_timeline"
        ]

    try:
        from databricks.sdk import WorkspaceClient
        from ._sql import run_sql
    except ImportError as exc:
        raise SystemExit("databricks-sdk is required. Install with: uv sync") from exc

    ws = WorkspaceClient()
    statements = _sql_statements(path.read_text())
    for statement in statements:
        run_sql(ws, statement, warehouse_id=wh_id)
    prefix = f" with prefix {table_prefix}" if table_prefix else ""
    return [
        "  apply  observability SQL "
        f"{catalog_name}.{schema_name}.apx_agent_timeline{prefix} "
        f"({len(statements)} statement{'s' if len(statements) != 1 else ''})"
    ]


def provision_memory_backends(
    *,
    pyproject_path: str | None = None,
) -> list[str]:
    """Provision memory and session backends declared in ``pyproject.toml``.

    **lakebase** is the only backend needing a status line here — nothing to
    provision: the instance is reached by ``host`` (a Lakebase project
    endpoint or instance DNS) with an OAuth token, and the per-agent tables
    auto-create on first use. Create the Lakebase database in the workspace
    and point ``host`` at it.

    Reads both ``[tool.apx.agent.memory]`` and ``[tool.apx.agent.session]`` —
    either block alone is enough to trigger the status line. Returns a list of
    status lines for printing.  No-ops silently when neither block is present.
    """
    from ._inspection import _load_agent_config  # noqa: PLC0415

    cfg = _load_agent_config(pyproject_path=pyproject_path)
    if cfg is None:
        return []

    mem = cfg.memory
    sess = getattr(cfg, "session", None)
    if mem is None and sess is None:
        return []

    lines: list[str] = []

    # Lakebase needs no provisioning step — connection is host + OAuth token and
    # tables auto-create on first use. Point host at an existing Lakebase database.
    if (mem and mem.type == "lakebase") or (sess and sess.type == "lakebase"):
        lines.append("  skip   lakebase — no provisioning needed (set host to your Lakebase endpoint)")

    return lines


__all__ = [
    "init_apps_experiment",
    "provision_lakehouse_observability",
    "provision_memory_backends",
]
