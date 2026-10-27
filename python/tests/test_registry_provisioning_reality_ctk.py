"""Ctk: the provisioning step's SQL carries the CREATE and both grant sets."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from ctk import Artifact, verify

from apx_agent._registry import RegistryTables, provision_registry


def test_rendered_provisioning_sql_is_real(tmp_path: Path, monkeypatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr("apx_agent._sql.run_sql", lambda ws, sql, **k: ran.append(" ".join(sql.split())) or [])
    ws = MagicMock()
    ws.tables.exists.return_value = MagicMock(table_exists=False)
    provision_registry(ws, tables=RegistryTables("c.apx.agent_registry", "c.apx.agent_tools"), sp_client_id="sp-1")
    out = tmp_path / "provision.sql"
    out.write_text("\n".join(ran) + "\n")
    verify(*(Artifact(str(out), min_bytes=400, must_contain=line) for line in (
        "CREATE TABLE IF NOT EXISTS c.apx.agent_registry",
        "CREATE TABLE IF NOT EXISTS c.apx.agent_tools",
        "GRANT SELECT ON TABLE c.apx.agent_registry TO `account users`",
        "GRANT SELECT, MODIFY ON TABLE c.apx.agent_registry TO `sp-1`",
        "GRANT SELECT, MODIFY ON TABLE c.apx.agent_tools TO `sp-1`",
    )))
