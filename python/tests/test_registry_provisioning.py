"""Agent registry location + provisioning (spec 2026-10-27)."""
from __future__ import annotations

from unittest.mock import MagicMock

import click
import pytest

from apx_agent import cli
from apx_agent._registry import (
    RegistryStepError,
    RegistryTables,
    provision_registry,
    registry_grants,
    resolve_registry_tables,
)

# Captured before conftest's autouse fixture swaps it for a no-op.
_REAL_DEPLOY_STEP = cli._deploy_registry_step


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



def test_registry_grants_cover_readers_and_the_app_sp():
    grants = registry_grants("c.apx.agent_registry", sp_client_id="1111-2222")
    assert [g.sql(g.privileges) for g in grants] == [
        "GRANT USE SCHEMA ON SCHEMA c.apx TO `account users`",
        "GRANT SELECT ON TABLE c.apx.agent_registry TO `account users`",
        "GRANT USE SCHEMA ON SCHEMA c.apx TO `1111-2222`",
        "GRANT SELECT, MODIFY ON TABLE c.apx.agent_registry TO `1111-2222`",
    ]


def test_no_sp_grants_only_readers():
    grants = registry_grants("c.apx.t", sp_client_id=None)
    assert {g.principal for g in grants} == {"account users"}


@pytest.mark.parametrize("bad", ["sp`; DROP TABLE x; --", "a b", ""])
def test_malformed_sp_id_is_rejected(bad):
    with pytest.raises(ValueError, match="service principal"):
        registry_grants("c.apx.t", sp_client_id=bad)


def test_malformed_table_is_rejected():
    with pytest.raises(ValueError):
        registry_grants("c.apx.t; DROP", sp_client_id=None)


_TABLES = RegistryTables("c.apx.agent_registry", "c.apx.agent_tools")
_ALL_HELD = {
    ("SCHEMA", "c.apx", "account users"): {"USE_SCHEMA"},
    ("SCHEMA", "c.apx", "sp-1"): {"USE_SCHEMA"},
    ("TABLE", "c.apx.agent_registry", "account users"): {"SELECT"},
    ("TABLE", "c.apx.agent_registry", "sp-1"): {"SELECT", "MODIFY"},
    ("TABLE", "c.apx.agent_tools", "account users"): {"SELECT"},
    ("TABLE", "c.apx.agent_tools", "sp-1"): {"SELECT", "MODIFY"},
}


def _provision_ws(monkeypatch, *, exists: bool, held=None, fail_on: tuple[str, ...] = ()):
    """Fake workspace: ``held`` maps (securable, name, principal) to privileges."""
    ran: list[str] = []
    held = held or {}

    def run_sql(ws, sql, **kwargs):
        flat = " ".join(sql.split())
        if any(f in flat for f in fail_on):
            raise RuntimeError("Query failed: PERMISSION_DENIED")
        ran.append(flat)
        return []

    def get_effective(securable_type, full_name, *, principal):
        privs = held.get((securable_type, full_name, principal), set())
        return MagicMock(privilege_assignments=[MagicMock(
            principal=principal, privileges=[MagicMock(privilege=MagicMock(value=p)) for p in privs],
        )])

    monkeypatch.setattr("apx_agent._sql.run_sql", run_sql)
    ws = MagicMock()
    ws.tables.exists.return_value = MagicMock(table_exists=exists)
    ws.grants.get_effective.side_effect = get_effective
    return ws, ran


def test_new_tables_are_created_and_fully_granted(monkeypatch):
    ws, ran = _provision_ws(monkeypatch, exists=False)
    provision_registry(ws, tables=_TABLES, sp_client_id="sp-1")
    creates = [s for s in ran if s.startswith("CREATE TABLE IF NOT EXISTS")]
    assert [s.split()[5] for s in creates] == ["c.apx.agent_registry", "c.apx.agent_tools"]
    assert "GRANT SELECT ON TABLE c.apx.agent_registry TO `account users`" in ran
    assert "GRANT SELECT, MODIFY ON TABLE c.apx.agent_tools TO `sp-1`" in ran
    assert ran.index("CREATE SCHEMA IF NOT EXISTS c.apx") < ran.index(creates[0])


def test_existing_tables_run_no_ddl(monkeypatch):
    ws, ran = _provision_ws(monkeypatch, exists=True, held=_ALL_HELD)
    provision_registry(ws, tables=_TABLES, sp_client_id="sp-1")
    assert not any(s.startswith("CREATE") for s in ran)


def test_redeploy_with_every_privilege_held_issues_no_grants(monkeypatch):
    """A non-owner redeploy must not re-send GRANTs it can't issue."""
    ws, ran = _provision_ws(monkeypatch, exists=True, held=_ALL_HELD, fail_on=("GRANT",))
    provision_registry(ws, tables=_TABLES, sp_client_id="sp-1")
    assert ran == []


def test_only_the_missing_privilege_is_granted(monkeypatch):
    held = {**_ALL_HELD, ("TABLE", "c.apx.agent_registry", "sp-1"): {"SELECT"}}
    ws, ran = _provision_ws(monkeypatch, exists=True, held=held)
    provision_registry(ws, tables=_TABLES, sp_client_id="sp-1")
    assert ran == ["GRANT MODIFY ON TABLE c.apx.agent_registry TO `sp-1`"]


def test_reader_grant_missing_after_a_failed_first_deploy_is_retried(monkeypatch):
    held = {k: v for k, v in _ALL_HELD.items() if k != ("TABLE", "c.apx.agent_registry", "account users")}
    ws, ran = _provision_ws(monkeypatch, exists=True, held=held)
    provision_registry(ws, tables=_TABLES, sp_client_id="sp-1")
    assert ran == ["GRANT SELECT ON TABLE c.apx.agent_registry TO `account users`"]


def test_all_failed_grants_are_reported_in_one_error(monkeypatch):
    ws, _ = _provision_ws(monkeypatch, exists=True, fail_on=("GRANT",))
    with pytest.raises(RegistryStepError) as exc:
        provision_registry(ws, tables=_TABLES, sp_client_id="sp-1")
    assert exc.value.step == "grant registry access"
    for statement in (
        "GRANT USE SCHEMA ON SCHEMA c.apx TO `account users`",
        "GRANT SELECT ON TABLE c.apx.agent_registry TO `account users`",
        "GRANT SELECT, MODIFY ON TABLE c.apx.agent_registry TO `sp-1`",
        "GRANT SELECT, MODIFY ON TABLE c.apx.agent_tools TO `sp-1`",
    ):
        assert statement in exc.value.fix
    assert "MANAGE" in str(exc.value)


def test_unreadable_catalog_during_existence_check_is_a_step_error(monkeypatch):
    ws, _ = _provision_ws(monkeypatch, exists=False)
    ws.tables.exists.side_effect = PermissionError("catalog 'c' is not accessible")
    with pytest.raises(RegistryStepError) as exc:
        provision_registry(ws, tables=_TABLES, sp_client_id=None)
    assert exc.value.step == "create the table"
    assert "not accessible" in str(exc.value)


def test_malformed_sp_id_is_rejected_before_any_sql(monkeypatch):
    ws, ran = _provision_ws(monkeypatch, exists=False)
    with pytest.raises(ValueError, match="service principal"):
        provision_registry(ws, tables=_TABLES, sp_client_id="x`y")
    assert ran == []


def test_record_in_registry_provisions_then_writes_row_and_tools(monkeypatch):
    order: list[tuple[str, ...]] = []
    monkeypatch.setattr("apx_agent._registry.provision_registry",
                        lambda ws, **k: order.append(("provision", k["sp_client_id"])))
    monkeypatch.setattr("apx_agent.publish_to_registry",
                        lambda **k: order.append(("row", k["registry_table"])) or "agent_x")
    monkeypatch.setattr("apx_agent._publish.publish_tools_to_registry",
                        lambda **k: order.append(("tools", k["agent_id"], k["tools_table"])) or 2)
    monkeypatch.setattr(cli, "_load_finalized_agent", lambda module: MagicMock())
    monkeypatch.setattr("apx_agent._resources._iter_tool_fns", lambda agent: [len, max])
    n = cli._record_in_registry(
        ws=MagicMock(), tables=RegistryTables("c.apx.agent_registry", "c.apx.agent_tools"),
        name="proof", display_name="proof", description="d", endpoint_url="https://proof",
        sp_client_id="sp-1", module="agent:agent", supervisor_agent_id=None, log=lambda m: None,
    )
    assert n == 2
    assert order == [("provision", "sp-1"), ("row", "c.apx.agent_registry"),
                     ("tools", "agent_x", "c.apx.agent_tools")]


def test_record_in_registry_wraps_row_failure(monkeypatch):
    monkeypatch.setattr("apx_agent._registry.provision_registry", lambda ws, **k: None)

    def boom(**k):
        raise PermissionError("not the owner")

    monkeypatch.setattr("apx_agent.publish_to_registry", boom)
    with pytest.raises(RegistryStepError) as exc:
        cli._record_in_registry(
            ws=MagicMock(), tables=RegistryTables("c.apx.agent_registry", "c.apx.agent_tools"),
            name="proof", display_name="proof", description="d", endpoint_url=None,
            sp_client_id=None, module=None, supervisor_agent_id=None, log=lambda m: None,
        )
    assert exc.value.step == "write the agent's row"
    assert "not the owner" in str(exc.value)


def test_deploy_registry_step_failure_exits_with_still_live(monkeypatch):
    monkeypatch.setattr(cli, "_connect_workspace", lambda profile: (MagicMock(), MagicMock(host="https://h")))

    def fail(**k):
        raise RegistryStepError(step="grant the App's service principal", table="c.apx.agent_registry",
                                cause=RuntimeError("PERMISSION_DENIED"), fix="ask its owner to run: GRANT ...")

    monkeypatch.setattr(cli, "_record_in_registry", fail)
    monkeypatch.setattr(cli, "_read_apx_agent_config", lambda: {"description": "d"})
    with pytest.raises(click.ClickException) as exc:
        _REAL_DEPLOY_STEP(profile="p", app_name="proof", app_url="https://proof",
                          tables=RegistryTables("c.apx.agent_registry", "c.apx.agent_tools"),
                          module="agent:agent", log=lambda m: None)
    assert "STILL LIVE" in exc.value.message and "ask its owner to run" in exc.value.message


def test_row_failure_that_is_not_ownership_points_at_modify(monkeypatch):
    monkeypatch.setattr("apx_agent._registry.provision_registry", lambda ws, **k: None)

    def denied(**k):
        raise RuntimeError("Query failed: PERMISSION_DENIED: MODIFY")

    monkeypatch.setattr("apx_agent.publish_to_registry", denied)
    with pytest.raises(RegistryStepError) as exc:
        cli._record_in_registry(
            ws=MagicMock(), tables=_TABLES, name="proof", display_name="proof", description="d",
            endpoint_url=None, sp_client_id=None, module=None, supervisor_agent_id=None, log=lambda m: None,
        )
    assert "MODIFY on c.apx.agent_registry" in exc.value.fix
    assert "another principal" not in exc.value.fix


def test_deploy_step_app_lookup_failure_still_says_still_live(monkeypatch):
    ws = MagicMock()
    ws.apps.get.side_effect = RuntimeError("apps API unavailable")
    monkeypatch.setattr(cli, "_connect_workspace", lambda profile: (ws, MagicMock()))
    monkeypatch.setattr(cli, "_read_apx_agent_config", lambda: {})
    with pytest.raises(click.ClickException) as exc:
        _REAL_DEPLOY_STEP(profile="p", app_name="proof", app_url=None, tables=_TABLES,
                          module="agent:agent", log=lambda m: None)
    assert "STILL LIVE" in exc.value.message and "apps API unavailable" in exc.value.message


def test_deploy_step_without_an_app_sp_fails_loudly(monkeypatch):
    ws = MagicMock()
    ws.apps.get.return_value = MagicMock(service_principal_client_id=None)
    monkeypatch.setattr(cli, "_connect_workspace", lambda profile: (ws, MagicMock()))
    monkeypatch.setattr(cli, "_read_apx_agent_config", lambda: {})
    recorded: list[str] = []
    monkeypatch.setattr(cli, "_record_in_registry", lambda **k: recorded.append("x") or 0)
    with pytest.raises(click.ClickException) as exc:
        _REAL_DEPLOY_STEP(profile="p", app_name="proof", app_url=None, tables=_TABLES,
                          module="agent:agent", log=lambda m: None)
    assert "STILL LIVE" in exc.value.message and "service principal" in exc.value.message
    assert recorded == []


def test_resolver_rejects_an_invalid_resolved_catalog(monkeypatch):
    ws = _ws_with_catalog(monkeypatch, "bad-catalog")
    with pytest.raises(RuntimeError, match="registry_table"):
        resolve_registry_tables(ws)


def test_resolver_wraps_sql_errors(monkeypatch):
    def run_sql(ws, sql, **kwargs):
        raise OSError("PermissionDenied: CAN_USE on warehouse")

    monkeypatch.setattr("apx_agent._sql.run_sql", run_sql)
    with pytest.raises(RuntimeError, match="CAN_USE"):
        resolve_registry_tables(MagicMock())
