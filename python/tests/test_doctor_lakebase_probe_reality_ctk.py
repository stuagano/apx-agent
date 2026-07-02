"""Claim-vs-reality: `doctor` must actually probe Lakebase, not trust config.

`check_memory_backend`'s docstring promises "Check that the configured memory
backend is reachable", and the delta branch backs that with a real `SELECT 1`.
But #349 removed the live Lakebase probe: the lakebase branch returned
`Status.OK` on mere config *presence*, checking the RAW `host` string without
resolving `${ENV_VAR}`. So `host = "${LAKEBASE_HOST}"` with the var unset (the
new scaffold default) reported OK while the runtime silently degraded sessions
and the durable checkpointer to in-process memory — losing history and pending
approvals on every restart (#355).

This reconciles the *claim* ("reachable") against reality: an unresolved /
empty host is WARN (not OK), a failed live probe is WARN, and only a real
`SELECT 1` round-trip earns OK — mirroring the delta branch.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apx_agent import AgentConfig, MemoryBackendConfig
from apx_agent._doctor import Status, check_memory_backend


def _cfg(**mem_kwargs: Any) -> AgentConfig:
    return AgentConfig(
        name="t",
        model="databricks-claude-sonnet-4-6",
        memory=MemoryBackendConfig(type="lakebase", **mem_kwargs),
    )


@contextmanager
def _doctor_env(cfg: AgentConfig):
    """Make check_memory_backend see `cfg` as the loaded config of an apx project."""
    with patch("apx_agent._doctor._is_apx_project", return_value=True), patch(
        "apx_agent._inspection._load_agent_config", return_value=cfg
    ):
        yield


@pytest.mark.unit
def test_unresolved_env_host_is_warn_not_ok(tmp_path, monkeypatch) -> None:
    # The scaffold default: host is an env ref that isn't exported.
    monkeypatch.delenv("LAKEBASE_HOST", raising=False)
    cfg = _cfg(host="${LAKEBASE_HOST}", database="databricks_postgres")
    with _doctor_env(cfg):
        check = check_memory_backend(tmp_path, auth_ok=True)
    assert check is not None
    assert check.status is Status.WARN, (
        f"unresolved ${{LAKEBASE_HOST}} reported {check.status} — the false-OK bug"
    )


@pytest.mark.unit
def test_unreachable_lakebase_is_warn_not_ok(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LAKEBASE_HOST", "nonexistent.database.example.com")
    cfg = _cfg(host="${LAKEBASE_HOST}", database="databricks_postgres")
    with _doctor_env(cfg), patch(
        "apx_agent._defaults._make_workspace_client", return_value=MagicMock()
    ), patch(
        "apx_agent._lakebase_engine.build_lakebase_engine",
        side_effect=RuntimeError("could not connect to host"),
    ):
        check = check_memory_backend(tmp_path, auth_ok=True)
    assert check is not None
    assert check.status is Status.WARN, f"unreachable lakebase reported {check.status}"


@pytest.mark.unit
def test_reachable_lakebase_is_ok(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LAKEBASE_HOST", "real.database.cloud.databricks.com")
    cfg = _cfg(host="${LAKEBASE_HOST}", database="databricks_postgres")

    # A live SELECT 1 succeeds only through a real engine connection.
    engine = MagicMock(name="engine")
    conn = MagicMock(name="conn")
    engine.connect.return_value.__enter__.return_value = conn

    with _doctor_env(cfg), patch(
        "apx_agent._defaults._make_workspace_client", return_value=MagicMock()
    ), patch(
        "apx_agent._lakebase_engine.build_lakebase_engine", return_value=engine
    ):
        check = check_memory_backend(tmp_path, auth_ok=True)

    assert check is not None
    assert check.status is Status.OK, f"reachable lakebase reported {check.status}"
    conn.execute.assert_called_once()  # a real SELECT 1 was issued
    engine.dispose.assert_called_once()  # the probe pool was not leaked
