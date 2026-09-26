"""Declared data governance — compile ``[tool.apx.agent.data]`` to UC ABAC DDL.

The ``data:`` block in ``AgentConfig`` declares governed tags, table tags, and
row-filter / column-mask policies. This module compiles that declaration to a
plan of SQL DDL statements, then applies them via ``run_sql``.

The plan is a pure function of the config — fully testable without a workspace.
``apply_data_governance`` is the only side-effecting entry point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ._models import DataConfig
from ._sql import run_sql

logger = logging.getLogger(__name__)


@dataclass
class DataGovernancePlan:
    """The compiled DDL plan for one agent's data-governance declaration."""

    governed_tag_ddl: list[str] = field(default_factory=list)
    table_tag_ddl: list[str] = field(default_factory=list)
    policy_ddl: list[str] = field(default_factory=list)

    @property
    def all_ddl(self) -> list[str]:
        """Every DDL statement in execution order."""
        return self.governed_tag_ddl + self.table_tag_ddl + self.policy_ddl


def _quote_ident(name: str) -> str:
    """Backtick-quote a UC identifier part."""
    return f"`{name}`"


def _quote_3part(name: str) -> str:
    """Backtick-quote a three-part UC name."""
    return ".".join(_quote_ident(p) for p in name.split("."))


def _sql_str(value: str) -> str:
    """Single-quote a SQL string literal, escaping internal quotes."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def compile_data_governance_plan(config: DataConfig) -> DataGovernancePlan:
    """Compile a ``DataConfig`` to a ``DataGovernancePlan`` of DDL strings.

    Pure function — no workspace calls, no side effects.
    """
    plan = DataGovernancePlan()

    # 1. Governed tags
    for tag in config.governed_tags:
        if tag.values is not None:
            values = ", ".join(_sql_str(v) for v in tag.values)
            plan.governed_tag_ddl.append(
                f"CREATE GOVERNED TAG IF NOT EXISTS {_quote_ident(tag.name)} VALUES ({values})"
            )
        else:
            plan.governed_tag_ddl.append(
                f"CREATE GOVERNED TAG IF NOT EXISTS {_quote_ident(tag.name)}"
            )

    # 2. Table tags
    for table in config.tables:
        pairs = ", ".join(
            f"{_sql_str(k)} = {_sql_str(v)}" for k, v in table.tags.items()
        )
        plan.table_tag_ddl.append(
            f"ALTER TABLE {_quote_3part(table.name)} SET TAGS ({pairs})"
        )

    # 3. Policies
    for table in config.tables:
        for policy in table.policies:
            parts: list[str] = [
                f"CREATE OR REPLACE POLICY {_quote_ident(policy.name)}",
                f"ON TABLE {_quote_3part(table.name)}",
            ]
            if policy.type == "row_filter":
                parts.append("ROW FILTER")
            else:
                parts.append("COLUMN MASK")
            parts.append(policy.function)
            parts.append(f"TO {_quote_ident(policy.to)}")
            if policy.when:
                parts.append(f"WHEN {policy.when}")
            if policy.match_columns:
                for k, v in policy.match_columns.items():
                    parts.append(
                        f"MATCH COLUMNS has_tag_value({_sql_str(k)}, {_sql_str(v)})"
                    )
            if policy.using_columns:
                cols = ", ".join(_quote_ident(c) for c in policy.using_columns)
                parts.append(f"USING COLUMNS ({cols})")
            plan.policy_ddl.append(" ".join(parts))

    return plan


def apply_data_governance(
    plan: DataGovernancePlan,
    ws: Any,
    *,
    warehouse_id: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Apply a compiled plan via ``run_sql``.

    Returns the list of DDL statements executed (or that would be executed
    when ``dry_run=True``). Raises on the first failure — governance DDL
    must not be silently skipped.
    """
    executed: list[str] = []
    for ddl in plan.all_ddl:
        if dry_run:
            logger.info("DRY RUN — would execute: %s", ddl)
        else:
            logger.info("Executing: %s", ddl)
            run_sql(ws, ddl, warehouse_id=warehouse_id)
        executed.append(ddl)
    return executed
