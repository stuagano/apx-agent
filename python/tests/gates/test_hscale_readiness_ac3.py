"""AC-3: local dev (no DATABRICKS_APP_PORT) or declared<=1 + in-memory boots, no raise."""
from __future__ import annotations

from apx_agent import AgentConfig

from .conftest import boot_app


def test_dev_boot_warns_only(monkeypatch, stub_startup) -> None:
    # Local dev: scaled declared but NOT on Apps => boots (warn-only, no raise).
    monkeypatch.setenv("APX_DECLARED_INSTANCES", "2")
    monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
    boot_app(AgentConfig(name="t"))  # no exception


def test_single_replica_on_apps_boots(monkeypatch, stub_startup) -> None:
    # On Apps but declared<=1: the single-replica case is fine, no raise.
    monkeypatch.setenv("APX_DECLARED_INSTANCES", "1")
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
    boot_app(AgentConfig(name="t"))  # no exception
