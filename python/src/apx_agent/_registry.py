"""Workspace agent registry: where it lives and how it is provisioned.

The registry (agent rows + tool rows) is a shared, workspace-wide resource. Its
location is explicit config when set, otherwise the workspace default catalog.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

REGISTRY_TABLE_ENV = "APX_REGISTRY_TABLE"
TOOLS_TABLE_ENV = "APX_TOOLS_TABLE"


@dataclass(frozen=True)
class RegistryTables:
    agents: str
    tools: str


def resolve_registry_tables(
    ws: "WorkspaceClient",
    *,
    warehouse_id: str | None = None,
    registry_table: str | None = None,
    tools_table: str | None = None,
) -> RegistryTables:
    """Explicit tables win; otherwise ``<current_catalog()>.apx.*``."""
    if registry_table and tools_table:
        return RegistryTables(registry_table, tools_table)
    from . import _sql

    rows = _sql.run_sql(ws, "SELECT current_catalog() AS catalog", warehouse_id=warehouse_id)
    catalog = rows[0]["catalog"] if rows else None
    if not catalog or catalog.lower() == "hive_metastore":
        raise RuntimeError(
            f"the workspace default catalog ({catalog!r}) can't hold the agent registry; "
            "set registry_table and tools_table in [tool.apx.deploy]"
        )
    return RegistryTables(
        registry_table or f"{catalog}.apx.agent_registry",
        tools_table or f"{catalog}.apx.agent_tools",
    )


_SP_ID = re.compile(r"^[A-Za-z0-9-]+$")


class RegistryStepError(RuntimeError):
    """A registry provisioning/recording step failed; carries the fix to show."""

    def __init__(self, *, step: str, table: str, cause: Exception, fix: str) -> None:
        self.step = step
        self.table = table
        self.fix = fix
        super().__init__(f"agent registry {table}: {step} failed: {cause}\n  fix: {fix}")


def registry_grant_sql(table: str, *, sp_client_id: str | None, created: bool) -> list[str]:
    """GRANTs for one registry table: readers on creation, the App SP always.

    ``USE SCHEMA`` rides with each table grant because UC table privileges are
    unusable without it. ``USE CATALOG`` is not granted: the deployer usually
    does not own the workspace default catalog.
    """
    from ._memory import validate_table_name

    validate_table_name(table)
    if sp_client_id is not None and not _SP_ID.match(sp_client_id):
        raise ValueError(f"invalid service principal id {sp_client_id!r}")
    schema = table.rsplit(".", 1)[0]
    sql: list[str] = []
    if created:
        sql += [
            f"GRANT USE SCHEMA ON SCHEMA {schema} TO `account users`",
            f"GRANT SELECT ON TABLE {table} TO `account users`",
        ]
    if sp_client_id is not None:
        sql += [
            f"GRANT USE SCHEMA ON SCHEMA {schema} TO `{sp_client_id}`",
            f"GRANT SELECT, MODIFY ON TABLE {table} TO `{sp_client_id}`",
        ]
    return sql


def provision_registry(
    ws: "WorkspaceClient",
    *,
    tables: RegistryTables,
    sp_client_id: str | None,
    warehouse_id: str | None = None,
) -> None:
    """Create both registry tables if missing, then apply the grants."""
    from . import _sql
    from ._publish import _REGISTRY_DDL, _TOOLS_DDL

    # Reject a malformed table name or SP id before any SQL runs.
    for table in (tables.agents, tables.tools):
        registry_grant_sql(table, sp_client_id=sp_client_id, created=True)
    for table, ddl in ((tables.agents, _REGISTRY_DDL), (tables.tools, _TOOLS_DDL)):
        schema = table.rsplit(".", 1)[0]
        try:
            created = not ws.tables.exists(full_name=table).table_exists
            _sql.run_sql(ws, f"CREATE SCHEMA IF NOT EXISTS {schema}", warehouse_id=warehouse_id)
            _sql.run_sql(ws, ddl.format(table=table), warehouse_id=warehouse_id)
        except Exception as e:
            raise RegistryStepError(
                step="create the table", table=table, cause=e,
                fix=(f"the deployer needs USE CATALOG and CREATE SCHEMA on {schema.split('.')[0]} "
                     "(or CREATE TABLE on the schema), or set registry_table and tools_table "
                     "in [tool.apx.deploy] to a schema you can write"),
            ) from e
        for statement in registry_grant_sql(table, sp_client_id=sp_client_id, created=created):
            try:
                _sql.run_sql(ws, statement, warehouse_id=warehouse_id)
            except Exception as e:
                reader = "account users" in statement
                raise RegistryStepError(
                    step="grant users read access" if reader else "grant the App's service principal",
                    table=table, cause=e,
                    fix=f"granting needs MANAGE on {table}; ask its owner to run: {statement}",
                ) from e
