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

from ._models import DataConfig, DataTableConfig, DataTagConfig
from ._sql import run_sql

logger = logging.getLogger(__name__)

# UC governed-tag keys may not contain reserved characters (``.``, ``=``), so
# the default taxonomy uses the UC-safe undotted form — same ``.`` → ``_``
# convention as ``uc_safe_tag_key`` for model-version tags.
DEFAULT_AGENT_TAG_MANAGED = "apx_agent_managed"
DEFAULT_AGENT_TAG_KIND = "apx_agent_kind"
DEFAULT_AGENT_KINDS = ["registry", "tools", "traces", "violations"]


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

    # 1. Governed tags. ``CREATE GOVERNED TAG`` has no ``IF NOT EXISTS`` /
    # ``OR REPLACE`` form on serverless — idempotency is handled at apply time
    # (see ``apply_data_governance``), not in the DDL text.
    for tag in config.governed_tags:
        if tag.values is not None:
            values = ", ".join(_sql_str(v) for v in tag.values)
            plan.governed_tag_ddl.append(
                f"CREATE GOVERNED TAG {_quote_ident(tag.name)} VALUES ({values})"
            )
        else:
            plan.governed_tag_ddl.append(
                f"CREATE GOVERNED TAG {_quote_ident(tag.name)}"
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


def _is_already_exists_error(exc: Exception) -> bool:
    """True when a governed-tag ``CREATE`` failed only because the tag exists.

    The statement-execution API surfaces the failure as a ``RuntimeError``
    whose message embeds either the prose (``... already exists``) or the
    Databricks error code (``TAG_ALREADY_EXISTS`` / underscore form).
    """
    msg = str(exc).lower()
    return "already exists" in msg or "already_exists" in msg


def apply_data_governance(
    plan: DataGovernancePlan,
    ws: Any,
    *,
    warehouse_id: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Apply a compiled plan via ``run_sql``.

    Returns the list of DDL statements executed (or that would be executed
    when ``dry_run=True``).

    Governed-tag ``CREATE`` statements tolerate "already exists" so re-deploys
    and tags shared across tables (the default ``apx_agent_*`` tags land on
    both the registry and tools tables) are idempotent — ``CREATE GOVERNED
    TAG`` has no ``IF NOT EXISTS`` form. Every other failure raises:
    governance DDL must not be silently skipped.
    """
    executed: list[str] = []

    def _run(ddl: str, *, tolerate_already_exists: bool) -> None:
        if dry_run:
            logger.info("DRY RUN — would execute: %s", ddl)
            return
        logger.info("Executing: %s", ddl)
        try:
            run_sql(ws, ddl, warehouse_id=warehouse_id)
        except Exception as e:
            if tolerate_already_exists and _is_already_exists_error(e):
                logger.info("Already exists, skipping: %s", ddl)
                return
            raise

    for ddl in plan.governed_tag_ddl:
        _run(ddl, tolerate_already_exists=True)
        executed.append(ddl)
    for ddl in plan.table_tag_ddl + plan.policy_ddl:
        _run(ddl, tolerate_already_exists=False)
        executed.append(ddl)
    return executed


def default_agent_tags_plan(table_name: str, *, kind: str) -> DataGovernancePlan:
    """Compile the default ``apx_agent_*`` governed-tag plan for one agent table.

    Every UC table an agent creates (violations, trace export, registry,
    tools) carries ``apx_agent_managed = 'true'`` and
    ``apx_agent_kind = '<kind>'`` so catalog/schema-level ABAC policies can
    target agent data even without an explicit ``[tool.apx.agent.data]``
    declaration. Raises ``ValueError`` for a kind outside the taxonomy —
    extending it means adding to ``DEFAULT_AGENT_KINDS``.
    """
    if kind not in DEFAULT_AGENT_KINDS:
        raise ValueError(
            f"unknown agent table kind {kind!r}; expected one of {DEFAULT_AGENT_KINDS}"
        )
    return compile_data_governance_plan(
        DataConfig(
            governed_tags=[
                DataTagConfig(name=DEFAULT_AGENT_TAG_MANAGED, values=["true"]),
                DataTagConfig(name=DEFAULT_AGENT_TAG_KIND, values=DEFAULT_AGENT_KINDS),
            ],
            tables=[
                DataTableConfig(
                    name=table_name,
                    tags={DEFAULT_AGENT_TAG_MANAGED: "true", DEFAULT_AGENT_TAG_KIND: kind},
                )
            ],
        )
    )


def apply_default_agent_tags(
    table_name: str,
    *,
    kind: str,
    ws: Any,
    warehouse_id: str | None = None,
) -> bool:
    """Best-effort default tagging for auto-created agent tables; True on success.

    Unlike declared data governance — which fails deploy closed — the default
    tag set is ambient: a workspace without ABAC support or a warehouse below
    the DBR 16.4/serverless floor must not break violation reporting, trace
    export, or registry publish. Failures log a warning and return ``False``;
    ``apx-agent doctor`` is the surfacing vehicle for the compute floor.
    """
    plan = default_agent_tags_plan(table_name, kind=kind)
    try:
        apply_data_governance(plan, ws, warehouse_id=warehouse_id)
    except Exception as e:
        logger.warning(
            "Default %s tags on %s not applied: %s — the table is untagged, so "
            "catalog/schema ABAC policies will not see it. Run `apx-agent doctor` "
            "to check the ABAC compute floor.",
            DEFAULT_AGENT_TAG_MANAGED, table_name, e,
        )
        return False
    return True
