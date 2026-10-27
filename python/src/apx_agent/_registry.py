"""Workspace agent registry: where it lives and how it is provisioned.

The registry (agent rows + tool rows) is a shared, workspace-wide resource. Its
location is explicit config when set, otherwise the workspace default catalog.
"""
from __future__ import annotations

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
