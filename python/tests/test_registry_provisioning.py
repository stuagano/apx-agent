"""Agent registry location + provisioning (spec 2026-10-27)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apx_agent._registry import RegistryTables, resolve_registry_tables


def _ws_with_catalog(monkeypatch: pytest.MonkeyPatch, catalog: str | None) -> MagicMock:
    calls: list[str] = []

    def run_sql(ws, sql, **kwargs):
        calls.append(sql)
        return [{"catalog": catalog}] if catalog is not None else []

    monkeypatch.setattr("apx_agent._sql.run_sql", run_sql)
    ws = MagicMock()
    ws.calls = calls
    return ws


def test_explicit_tables_win_without_sql(monkeypatch):
    ws = _ws_with_catalog(monkeypatch, "ignored")
    tables = resolve_registry_tables(ws, registry_table="a.b.reg", tools_table="a.b.tools")
    assert tables == RegistryTables("a.b.reg", "a.b.tools")
    assert ws.calls == []


def test_defaults_to_workspace_default_catalog(monkeypatch):
    ws = _ws_with_catalog(monkeypatch, "workspace_cat")
    tables = resolve_registry_tables(ws)
    assert tables == RegistryTables("workspace_cat.apx.agent_registry", "workspace_cat.apx.agent_tools")
    assert ws.calls == ["SELECT current_catalog() AS catalog"]


def test_one_explicit_table_keeps_it_and_resolves_the_other(monkeypatch):
    ws = _ws_with_catalog(monkeypatch, "workspace_cat")
    tables = resolve_registry_tables(ws, registry_table="x.y.reg")
    assert tables == RegistryTables("x.y.reg", "workspace_cat.apx.agent_tools")


@pytest.mark.parametrize("catalog", [None, "", "hive_metastore", "HIVE_METASTORE"])
def test_unusable_default_catalog_is_refused(monkeypatch, catalog):
    ws = _ws_with_catalog(monkeypatch, catalog)
    with pytest.raises(RuntimeError, match=r"set registry_table and tools_table in \[tool.apx.deploy\]"):
        resolve_registry_tables(ws)
