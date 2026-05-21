"""Bootstrap helpers for apx-agent Apps deployments.

This module hosts the small amount of pre-deploy plumbing that every
``apx scaffold --target apps`` project needs: creating an MLflow experiment
at the canonical workspace path and writing its id into a local ``.env`` so
the bundle and the agent module pick it up.

The scaffolded ``scripts/quickstart.py`` is a thin wrapper around
:func:`init_apps_experiment` — the logic lives here so we don't ship the
same 80-line script into every example.
"""

from __future__ import annotations

import os
from pathlib import Path


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
    return os.environ.get("BUNDLE_TARGET", "dev")


def _resolve_agent_name(agent_name: str | None) -> str:
    """Agent name — defaults to the cwd directory name."""
    if agent_name:
        return agent_name
    return Path.cwd().name


def _resolve_tracking_uri(tracking_uri: str | None) -> str:
    """Tracking URI — defaults to ``MLFLOW_TRACKING_URI`` env, then ``"databricks"``."""
    if tracking_uri:
        return tracking_uri
    return os.environ.get("MLFLOW_TRACKING_URI", "databricks")


def _ensure_experiment(experiment_path: str, tracking_uri: str) -> str:
    """Create or reuse the MLflow experiment at ``experiment_path``.

    Returns the experiment id. The ``mlflow`` import is deferred so this
    module stays cheap to import in environments without mlflow installed
    (tests, doc tooling).
    """
    try:
        import mlflow
    except ImportError as exc:
        raise SystemExit("mlflow is required. Install with: uv sync") from exc

    mlflow.set_tracking_uri(tracking_uri)

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
) -> tuple[str, str]:
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
    experiment_id = _ensure_experiment(experiment_path, resolved_tracking)

    resolved_env_path = Path(env_path) if env_path else Path.cwd() / ".env"
    _write_experiment_id(resolved_env_path, experiment_id)

    return experiment_path, experiment_id


__all__ = ["init_apps_experiment"]
