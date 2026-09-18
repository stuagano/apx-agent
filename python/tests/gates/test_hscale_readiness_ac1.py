"""AC-1: compile/deploy refuses a scaled + in-memory app, naming type='lakebase'."""
from __future__ import annotations

import pytest

from apx_agent import AgentConfig
from apx_agent._models import DeployConfig, SessionBackendConfig
from apx_agent._project_gen import _build_databricks_yml


def test_compile_refuses_scaled_in_memory() -> None:
    # No session block => in-memory (checkpointer resolves to None).
    cfg = AgentConfig(name="t", deploy=DeployConfig(instances=2))
    with pytest.raises(ValueError, match="type='lakebase'"):
        _build_databricks_yml(cfg)

    # Explicit type='inmemory' => same refusal.
    cfg2 = AgentConfig(
        name="t",
        deploy=DeployConfig(instances=2),
        session=SessionBackendConfig(type="inmemory"),
    )
    with pytest.raises(ValueError, match="type='lakebase'"):
        _build_databricks_yml(cfg2)
