"""AC-4: scaled + type='lakebase' is clean at compile; runtime only if backends resolve."""
from __future__ import annotations

import pytest

from apx_agent import AgentConfig
from apx_agent._models import DeployConfig, SessionBackendConfig
from apx_agent._project_gen import _build_databricks_yml

from .conftest import boot_app


def _lakebase_scaled() -> AgentConfig:
    return AgentConfig(
        name="t",
        deploy=DeployConfig(instances=3),
        session=SessionBackendConfig(
            type="lakebase", host="${LAKEBASE_HOST}", database="apx"
        ),
    )


def test_scaled_lakebase_compile_clean() -> None:
    # Compile: declared lakebase is durable-intent even with ws=None.
    yml = _build_databricks_yml(_lakebase_scaled())
    assert "bundle:" in yml


def test_scaled_lakebase_boot_raises_without_resolved_backends(
    monkeypatch, stub_startup
) -> None:
    # Runtime must not trust the declaration: ws=None means no Lakebase
    # checkpointer actually built, so this is still process-local.
    monkeypatch.setenv("APX_DECLARED_INSTANCES", "3")
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
    with pytest.raises(RuntimeError, match="type='lakebase'"):
        boot_app(_lakebase_scaled())


def test_scaled_lakebase_boots_when_backends_resolve(monkeypatch, stub_startup) -> None:
    # A resolved store + checkpointer is the real durable path.
    monkeypatch.setenv("APX_DECLARED_INSTANCES", "3")
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
    monkeypatch.setattr(
        "apx_agent._memory_wiring.resolve_conversation_store",
        lambda *a, **k: object(),
    )
    monkeypatch.setattr(
        "apx_agent._memory_wiring.resolve_checkpointer",
        lambda *a, **k: object(),
    )
    boot_app(_lakebase_scaled())
