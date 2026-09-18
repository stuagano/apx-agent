"""AC-4: scaled + type='lakebase' is clean at both catch points (compile + boot)."""
from __future__ import annotations

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


def test_scaled_lakebase_boots_clean(monkeypatch, stub_startup) -> None:
    # Compile: no raise (declared lakebase is durable-intent even with ws=None).
    yml = _build_databricks_yml(_lakebase_scaled())
    assert "bundle:" in yml

    # Runtime: on Apps, declared 3, lakebase => no raise.
    monkeypatch.setenv("APX_DECLARED_INSTANCES", "3")
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
    boot_app(_lakebase_scaled())  # no exception
