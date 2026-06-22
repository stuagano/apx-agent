"""Tests for ``apx_agent.bootstrap.init_apps_experiment``.

Covers the three things the scaffolded quickstart actually does:

  1. Creation path — when no experiment exists, ``create_experiment`` is
     called with the canonical ``/Users/<user>/<agent>-<target>`` path and
     the returned id is written to ``.env``.
  2. ``.env`` preservation — pre-existing lines unrelated to
     ``MLFLOW_EXPERIMENT_ID`` survive the rewrite.
  3. Idempotent re-run — when the experiment already exists, the existing
     id is reused (no duplicate creation) and the ``.env`` file ends up with
     a single ``MLFLOW_EXPERIMENT_ID=`` line.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apx_agent.bootstrap import init_apps_experiment, provision_memory_backends


@pytest.fixture
def fake_mlflow(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Install a stub ``mlflow`` module in ``sys.modules``.

    ``init_apps_experiment`` imports mlflow lazily, so we can fake the whole
    surface (``set_tracking_uri`` + ``tracking.MlflowClient``) without
    requiring mlflow to be installed in the test env. Returns the namespace
    so individual tests can inspect calls / set return values.
    """
    client = MagicMock()
    # Default to "no existing experiment" — tests override per-case.
    client.get_experiment_by_name.return_value = None
    client.create_experiment.return_value = "exp-123"

    tracking_mod = types.SimpleNamespace(MlflowClient=lambda: client)
    fake = types.SimpleNamespace(
        set_tracking_uri=MagicMock(),
        tracking=tracking_mod,
    )
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    # init_apps_experiment also references mlflow.tracking — make sure the
    # attribute lookup ``mlflow.tracking.MlflowClient`` resolves through the
    # stub namespace.
    return types.SimpleNamespace(mlflow=fake, client=client)


# ---------------------------------------------------------------------------
# Test 1: creation path — experiment is created and .env gets the id
# ---------------------------------------------------------------------------


def test_init_creates_experiment_and_writes_env(
    fake_mlflow: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force a deterministic user/target so the experiment path is predictable.
    monkeypatch.setenv("DATABRICKS_USER", "alice@example.com")
    monkeypatch.setenv("BUNDLE_TARGET", "dev")
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)

    env_file = tmp_path / ".env"
    path, exp_id = init_apps_experiment(
        agent_name="my-agent",
        env_path=str(env_file),
    )

    assert path == "/Users/alice@example.com/my-agent-dev"
    assert exp_id == "exp-123"

    # MLflow client was asked to create the experiment with the right name.
    fake_mlflow.client.get_experiment_by_name.assert_called_once_with(
        "/Users/alice@example.com/my-agent-dev"
    )
    fake_mlflow.client.create_experiment.assert_called_once_with(
        "/Users/alice@example.com/my-agent-dev"
    )
    # Default tracking URI is "databricks".
    fake_mlflow.mlflow.set_tracking_uri.assert_called_once_with("databricks")

    # .env now contains the id.
    contents = env_file.read_text()
    assert "MLFLOW_EXPERIMENT_ID=exp-123" in contents


# ---------------------------------------------------------------------------
# Test 2: .env write preserves existing lines
# ---------------------------------------------------------------------------


def test_init_preserves_existing_env_lines(
    fake_mlflow: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRICKS_USER", "alice")
    monkeypatch.setenv("BUNDLE_TARGET", "dev")

    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABRICKS_CONFIG_PROFILE=DEFAULT\n"
        "SOME_OTHER_VAR=keep-me\n"
        "MLFLOW_EXPERIMENT_ID=stale-id\n"
    )

    init_apps_experiment(agent_name="my-agent", env_path=str(env_file))

    lines = env_file.read_text().splitlines()
    # Unrelated lines survive.
    assert "DATABRICKS_CONFIG_PROFILE=DEFAULT" in lines
    assert "SOME_OTHER_VAR=keep-me" in lines
    # Old experiment id is replaced, not duplicated.
    exp_lines = [l for l in lines if l.startswith("MLFLOW_EXPERIMENT_ID=")]
    assert exp_lines == ["MLFLOW_EXPERIMENT_ID=exp-123"]


# ---------------------------------------------------------------------------
# Test 3: idempotent re-run — existing experiment reused, single id line
# ---------------------------------------------------------------------------


def test_init_is_idempotent_when_experiment_exists(
    fake_mlflow: types.SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRICKS_USER", "alice")
    monkeypatch.setenv("BUNDLE_TARGET", "dev")

    # Pretend the experiment already exists with id "exp-existing".
    existing = MagicMock()
    existing.experiment_id = "exp-existing"
    fake_mlflow.client.get_experiment_by_name.return_value = existing

    env_file = tmp_path / ".env"

    # First run — no .env yet.
    path1, id1 = init_apps_experiment(
        agent_name="my-agent", env_path=str(env_file)
    )
    # Second run — same fixture, .env now exists.
    path2, id2 = init_apps_experiment(
        agent_name="my-agent", env_path=str(env_file)
    )

    assert path1 == path2 == "/Users/alice/my-agent-dev"
    assert id1 == id2 == "exp-existing"

    # create_experiment was never called — we reused the existing one both times.
    fake_mlflow.client.create_experiment.assert_not_called()

    # And the .env file has exactly one MLFLOW_EXPERIMENT_ID line.
    lines = env_file.read_text().splitlines()
    exp_lines = [l for l in lines if l.startswith("MLFLOW_EXPERIMENT_ID=")]
    assert exp_lines == ["MLFLOW_EXPERIMENT_ID=exp-existing"]


# ---------------------------------------------------------------------------
# Test 4: provision_memory_backends reaches lakebase create for a SESSION-only
# config (no [tool.apx.agent.memory] block). Regression guard — the old
# `cfg.memory is None` early-return killed session-only provisioning.
# ---------------------------------------------------------------------------


def _fake_session_lakebase_config() -> types.SimpleNamespace:
    """Config with a lakebase session block and no memory block."""
    session = types.SimpleNamespace(
        type="lakebase", instance_name="apx-agent", database="my_agent", table_name=None
    )
    return types.SimpleNamespace(memory=None, session=session)


def test_provision_session_only_lakebase_creates_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session-only lakebase config provisions the instance (no memory block)."""
    import apx_agent._inspection as _inspection
    import apx_agent._defaults as _defaults

    monkeypatch.setattr(
        _inspection, "_load_agent_config", lambda **_: _fake_session_lakebase_config()
    )

    ws = MagicMock()
    # Instance does not exist yet → get raises, create succeeds.
    ws.database.get_database_instance.side_effect = RuntimeError("not found")
    ws.database.create_database_instance_and_wait.return_value = types.SimpleNamespace(
        read_write_dns="apx-agent.db.databricks.com"
    )
    monkeypatch.setattr(_defaults, "_make_workspace_client", lambda: ws)

    lines = provision_memory_backends()

    # The create call actually fired (the bug was: it never reached here).
    ws.database.create_database_instance_and_wait.assert_called_once()
    created = ws.database.create_database_instance_and_wait.call_args[0][0]
    assert created.name == "apx-agent"
    assert any("done   lakebase instance 'apx-agent'" in l for l in lines), lines


def test_provision_reuses_existing_lakebase_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the shared instance already exists, no create call is made (idempotent)."""
    import apx_agent._inspection as _inspection
    import apx_agent._defaults as _defaults

    monkeypatch.setattr(
        _inspection, "_load_agent_config", lambda **_: _fake_session_lakebase_config()
    )
    ws = MagicMock()
    ws.database.get_database_instance.return_value = types.SimpleNamespace(
        read_write_dns="apx-agent.db.databricks.com"
    )
    monkeypatch.setattr(_defaults, "_make_workspace_client", lambda: ws)

    lines = provision_memory_backends()

    ws.database.create_database_instance_and_wait.assert_not_called()
    assert any("exists lakebase instance 'apx-agent'" in l for l in lines), lines
