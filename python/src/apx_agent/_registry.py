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
    from ._memory import validate_table_name

    try:
        rows = _sql.run_sql(ws, "SELECT current_catalog() AS catalog", warehouse_id=warehouse_id)
    except Exception as e:
        raise RuntimeError(f"could not resolve the workspace default catalog for the agent registry: {e}") from e
    catalog = rows[0]["catalog"] if rows else None
    if not catalog or catalog.lower() == "hive_metastore":
        raise RuntimeError(
            f"the workspace default catalog ({catalog!r}) can't hold the agent registry; "
            "set registry_table and tools_table in [tool.apx.deploy]"
        )
    tables = RegistryTables(
        registry_table or f"{catalog}.apx.agent_registry",
        tools_table or f"{catalog}.apx.agent_tools",
    )
    try:
        validate_table_name(tables.agents)
        validate_table_name(tables.tools)
    except ValueError as e:
        raise RuntimeError(
            f"{e} — set registry_table and tools_table in [tool.apx.deploy]"
        ) from e
    return tables


_SP_ID = re.compile(r"^[A-Za-z0-9-]+$")


class RegistryStepError(RuntimeError):
    """A registry provisioning/recording step failed; carries the fix to show."""

    def __init__(self, *, step: str, table: str, cause: Exception, fix: str) -> None:
        self.step = step
        self.table = table
        self.fix = fix
        super().__init__(f"agent registry {table}: {step} failed: {cause}\n  fix: {fix}")


@dataclass(frozen=True)
class Grant:
    """One desired UC grant: ``privileges`` on a securable for a principal."""

    securable: str  # "CATALOG" | "SCHEMA" | "TABLE"
    name: str
    principal: str
    privileges: frozenset[str]

    def sql(self, privileges: frozenset[str]) -> str:
        order = ("USE_CATALOG", "USE_SCHEMA", "SELECT", "MODIFY")
        listed = ", ".join(p.replace("_", " ") for p in order if p in privileges)
        return f"GRANT {listed} ON {self.securable} {self.name} TO `{self.principal}`"


def registry_grants(table: str, *, sp_client_id: str | None) -> list[Grant]:
    """Everything one registry table needs: readers, then the App SP.

    ``USE CATALOG`` and ``USE SCHEMA`` ride with each table grant because UC
    table privileges are unusable without them. Only missing privileges are
    granted, so a deployer who can't grant on the catalog fails only when a
    principal actually lacks ``USE CATALOG``.
    """
    from ._memory import validate_table_name

    validate_table_name(table)
    if sp_client_id is not None and not _SP_ID.match(sp_client_id):
        raise ValueError(f"invalid service principal id {sp_client_id!r}")
    schema = table.rsplit(".", 1)[0]
    catalog = table.split(".", 1)[0]
    principals = [("account users", frozenset({"SELECT"}))]
    if sp_client_id is not None:
        principals.append((sp_client_id, frozenset({"SELECT", "MODIFY"})))
    grants: list[Grant] = []
    for principal, table_privs in principals:
        grants += [
            Grant("CATALOG", catalog, principal, frozenset({"USE_CATALOG"})),
            Grant("SCHEMA", schema, principal, frozenset({"USE_SCHEMA"})),
            Grant("TABLE", table, principal, table_privs),
        ]
    return grants


def _held(ws: "WorkspaceClient", grant: Grant) -> frozenset[str]:
    """Privileges ``grant.principal`` already holds on the securable (incl. inherited)."""
    response = ws.grants.get_effective(grant.securable, grant.name, principal=grant.principal)
    held = {
        privilege.privilege.value
        for assignment in response.privilege_assignments or []
        for privilege in assignment.privileges or []
        if privilege.privilege is not None
    }
    return grant.privileges if "ALL_PRIVILEGES" in held else frozenset(held)


def provision_registry(
    ws: "WorkspaceClient",
    *,
    tables: RegistryTables,
    sp_client_id: str | None,
    warehouse_id: str | None = None,
) -> None:
    """Create missing registry tables, then grant only what is missing.

    UC ``GRANT`` needs owner/MANAGE even for a privilege already held, so a
    redeploy by a non-owner must not re-send grants that are already in place.
    Every grant that still fails is reported together.
    """
    from . import _sql
    from ._publish import _REGISTRY_DDL, _TOOLS_DDL

    # Reject a malformed table name or SP id before any SQL runs.
    plans = [
        (table, ddl, registry_grants(table, sp_client_id=sp_client_id))
        for table, ddl in ((tables.agents, _REGISTRY_DDL), (tables.tools, _TOOLS_DDL))
    ]
    failed: list[str] = []
    causes: list[str] = []
    for table, ddl, grants in plans:
        schema = table.rsplit(".", 1)[0]
        try:
            if not ws.tables.exists(full_name=table).table_exists:
                _sql.run_sql(ws, f"CREATE SCHEMA IF NOT EXISTS {schema}", warehouse_id=warehouse_id)
                _sql.run_sql(ws, ddl.format(table=table), warehouse_id=warehouse_id)
        except Exception as e:
            raise RegistryStepError(
                step="create the table", table=table, cause=e,
                fix=(f"the deployer needs USE CATALOG and CREATE SCHEMA on {schema.split('.')[0]} "
                     "(or CREATE TABLE on the schema), or set registry_table and tools_table "
                     "in [tool.apx.deploy] to a schema you can write"),
            ) from e
        for grant in grants:
            try:
                missing = grant.privileges - _held(ws, grant)
            except Exception as e:  # can't read grants: try the full grant
                causes.append(f"reading grants on {grant.name}: {e}")
                missing = grant.privileges
            if not missing:
                continue
            statement = grant.sql(missing)
            try:
                _sql.run_sql(ws, statement, warehouse_id=warehouse_id)
            except Exception as e:
                failed.append(statement)
                causes.append(str(e))
    failed = list(dict.fromkeys(failed))  # both tables share the catalog grant
    if failed:
        raise RegistryStepError(
            step="grant registry access", table=tables.agents,
            cause=RuntimeError("; ".join(causes)),
            fix=("granting needs MANAGE (or ownership) on the registry catalog, schema and tables; "
                 "ask their owner to run:\n    " + "\n    ".join(failed)),
        )
