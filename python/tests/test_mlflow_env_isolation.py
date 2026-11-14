"""#1190: a test that sets MLFLOW_EXPERIMENT_ID (as mlflow.set_experiment does) must not leak it."""

from __future__ import annotations

import os

import mlflow


def test_set_experiment_writes_the_env_var() -> None:
    mlflow.set_experiment("apx-env-isolation-probe")
    assert os.environ.get("MLFLOW_EXPERIMENT_ID")


def test_the_next_test_starts_without_it() -> None:
    # Same file, so it runs after the test above in a serial run.
    assert "MLFLOW_EXPERIMENT_ID" not in os.environ
