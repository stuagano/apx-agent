"""``mlflow.set_experiment`` writes MLFLOW_EXPERIMENT_ID into os.environ; it must not outlive the test (#1190)."""

import os

import mlflow


def test_a_set_experiment_writes_the_env_var() -> None:
    mlflow.set_experiment("apx-env-isolation-probe")
    assert os.environ.get("MLFLOW_EXPERIMENT_ID")


def test_b_next_test_starts_without_it() -> None:
    # Meaningful when run after test_a on the same worker (serially, -p no:xdist).
    assert "MLFLOW_EXPERIMENT_ID" not in os.environ
