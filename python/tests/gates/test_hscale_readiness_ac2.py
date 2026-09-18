"""AC-2: prod boot (on Apps + APX_DECLARED_INSTANCES>1 + in-memory) refuses to boot."""
from __future__ import annotations

import pytest

from apx_agent import AgentConfig

from .conftest import boot_app


def test_prod_boot_refuses_scaled_in_memory(monkeypatch, stub_startup) -> None:
    monkeypatch.setenv("APX_DECLARED_INSTANCES", "2")
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")  # => _is_deployed_app()

    with pytest.raises(RuntimeError, match="type='lakebase'"):
        boot_app(AgentConfig(name="t"))
