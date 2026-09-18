"""AC-3: local dev (no DATABRICKS_APP_PORT) or declared<=1 + in-memory boots, no raise."""
from __future__ import annotations

import logging

from apx_agent import AgentConfig

from .conftest import boot_app

_INMEMORY_LOG = "Short-term memory: in-process (InMemorySaver)"


def test_dev_boot_warns_only(monkeypatch, stub_startup, caplog) -> None:
    # Local dev: scaled declared but NOT on Apps => boots (info-only, no raise).
    monkeypatch.setenv("APX_DECLARED_INSTANCES", "2")
    monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
    with caplog.at_level(logging.INFO, logger="apx_agent._chat_agent"):
        boot_app(AgentConfig(name="t"))
    assert _INMEMORY_LOG in caplog.text


def test_single_replica_on_apps_boots(monkeypatch, stub_startup) -> None:
    # On Apps but declared<=1: the single-replica case is fine, no raise.
    monkeypatch.setenv("APX_DECLARED_INSTANCES", "1")
    monkeypatch.setenv("DATABRICKS_APP_PORT", "8000")
    boot_app(AgentConfig(name="t"))  # no exception
