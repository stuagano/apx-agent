"""Agent registry location + provisioning (spec 2026-10-27)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apx_agent._registry import (
    RegistryStepError,
    RegistryTables,
    provision_registry,
    registry_grant_sql,
    resolve_registry_tables,
)


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



def test_grants_on_creation_include_account_users_and_sp():
    sql = registry_grant_sql("c.apx.agent_registry", sp_client_id="1111-2222", created=True)
    assert sql == [
        "GRANT USE SCHEMA ON SCHEMA c.apx TO `account users`",
        "GRANT SELECT ON TABLE c.apx.agent_registry TO `account users`",
        "GRANT USE SCHEMA ON SCHEMA c.apx TO `1111-2222`",
        "GRANT SELECT, MODIFY ON TABLE c.apx.agent_registry TO `1111-2222`",
    ]


def test_existing_table_only_grants_the_sp():
    sql = registry_grant_sql("c.apx.agent_registry", sp_client_id="1111-2222", created=False)
    assert all("account users" not in s for s in sql)
    assert "GRANT SELECT, MODIFY ON TABLE c.apx.agent_registry TO `1111-2222`" in sql


def test_no_sp_and_existing_table_grants_nothing():
    assert registry_grant_sql("c.apx.t", sp_client_id=None, created=False) == []


@pytest.mark.parametrize("bad", ["sp`; DROP TABLE x; --", "a b", ""])
def test_malformed_sp_id_is_rejected(bad):
    with pytest.raises(ValueError, match="service principal"):
        registry_grant_sql("c.apx.t", sp_client_id=bad, created=False)


def test_malformed_table_is_rejected():
    with pytest.raises(ValueError):
        registry_grant_sql("c.apx.t; DROP", sp_client_id=None, created=True)


def _provision_ws(monkeypatch, *, exists: bool, fail_on: str | None = None):
    ran: list[str] = []

    def run_sql(ws, sql, **kwargs):
        if fail_on and fail_on in sql:
            raise RuntimeError("Query failed: PERMISSION_DENIED")
        ran.append(" ".join(sql.split()))
        return []

    monkeypatch.setattr("apx_agent._sql.run_sql", run_sql)
    ws = MagicMock()
    ws.tables.exists.return_value = MagicMock(table_exists=exists)
    return ws, ran


def test_provision_creates_then_grants_both_tables(monkeypatch):
    ws, ran = _provision_ws(monkeypatch, exists=False)
    provision_registry(ws, tables=RegistryTables("c.apx.agent_registry", "c.apx.agent_tools"), sp_client_id="sp-1")
    creates = [s for s in ran if s.startswith("CREATE TABLE IF NOT EXISTS")]
    assert [s.split()[5] for s in creates] == ["c.apx.agent_registry", "c.apx.agent_tools"]
    assert "GRANT SELECT ON TABLE c.apx.agent_registry TO `account users`" in ran
    assert "GRANT SELECT, MODIFY ON TABLE c.apx.agent_tools TO `sp-1`" in ran
    assert ran.index("CREATE SCHEMA IF NOT EXISTS c.apx") < ran.index(creates[0])


def test_grant_without_manage_names_the_fix(monkeypatch):
    ws, _ = _provision_ws(monkeypatch, exists=True, fail_on="MODIFY")
    with pytest.raises(RegistryStepError) as exc:
        provision_registry(ws, tables=RegistryTables("c.apx.agent_registry", "c.apx.agent_tools"), sp_client_id="sp-1")
    assert exc.value.step == "grant the App's service principal"
    assert exc.value.table == "c.apx.agent_registry"
    assert "GRANT SELECT, MODIFY ON TABLE c.apx.agent_registry TO `sp-1`" in exc.value.fix
    assert "MANAGE" in str(exc.value)


def test_unreadable_catalog_during_existence_check_is_a_step_error(monkeypatch):
    ws, _ = _provision_ws(monkeypatch, exists=False)
    ws.tables.exists.side_effect = PermissionError("catalog 'c' is not accessible")
    with pytest.raises(RegistryStepError) as exc:
        provision_registry(ws, tables=RegistryTables("c.apx.agent_registry", "c.apx.agent_tools"), sp_client_id=None)
    assert exc.value.step == "create the table"
    assert "not accessible" in str(exc.value)


def test_malformed_sp_id_is_rejected_before_any_sql(monkeypatch):
    ws, ran = _provision_ws(monkeypatch, exists=False)
    with pytest.raises(ValueError, match="service principal"):
        provision_registry(ws, tables=RegistryTables("c.apx.agent_registry", "c.apx.agent_tools"), sp_client_id="x`y")
    assert ran == []
