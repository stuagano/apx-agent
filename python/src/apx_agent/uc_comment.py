"""uc_comment_tool — a governed, opt-in tool to write Unity Catalog COMMENTs.

Writes table/column comments via the SQL warehouse under the CALLING USER's
identity (OBO), so the write requires their UC ``MODIFY`` grant and is audited.
Scoped to a declared ``catalog.schema``. Opt-in only — declare it via
``[[tool.apx.tools]] type = "uc_comment_writer"``; never wired by default.

Annotations are intentionally NOT deferred so ``UserClientDependency`` resolves
eagerly (see sql_tools.py for the rationale).
"""

import logging
import re
from typing import Any

from ._sql import run_sql

logger = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _esc_literal(text: str) -> str:
    from ._sql_escape import sql_escape  # canonical escaper (#351/#352)

    return sql_escape(text)


def uc_comment_tool(
    *,
    catalog: str,
    schema: str,
    warehouse_id: str | None = None,
    name: str = "update_uc_comment",
    description: str | None = None,
) -> Any:
    """Return an opt-in tool that writes a UC table/column COMMENT.

    Scoped to ``catalog.schema``. Runs as the calling user (OBO) — the write
    needs their UC ``MODIFY`` grant. Args the agent supplies: ``table`` (within
    the declared schema), optional ``column``, and the ``comment`` text.
    """
    from ._defaults import UserClientDependency
    from ._resources import ResourceSpec
    from ._tool_factory import build_tool

    _desc = description or (
        f"Write a Unity Catalog COMMENT on a table or column in "
        f"`{catalog}.{schema}`. Runs as the calling user — requires their UC "
        f"MODIFY grant. Provide `table`, an optional `column`, and the `comment` text."
    )

    def _build_stmt(table: str | None, comment: str | None, column: str | None) -> str:
        fqn = f"`{catalog}`.`{schema}`.`{table}`"
        lit = _esc_literal(comment or "")
        if column is None:
            return f"COMMENT ON TABLE {fqn} IS '{lit}'"
        return f"ALTER TABLE {fqn} ALTER COLUMN `{column}` COMMENT '{lit}'"

    def _bad_ident(table: str | None, column: str | None) -> str | None:
        if not _IDENT.match(table or ""):
            return f"invalid table identifier: {table!r}"
        if column is not None and not _IDENT.match(column):
            return f"invalid column identifier: {column!r}"
        return None

    async def _update_uc_comment(
        ws: UserClientDependency,  # type: ignore[valid-type]
        table: str | None = None,
        comment: str | None = None,
        column: str | None = None,
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Placeholder doc — overwritten below."""
        if comments is not None and table is not None:
            return {"status": "error",
                    "message": "provide either (table, comment) or comments=[...], not both"}
        if comments is None:
            if table is None or comment is None:
                return {"status": "error",
                        "message": "provide (table, comment) or comments=[...]"}
            err = _bad_ident(table, column)
            if err is not None:
                return {"status": "error", "message": err}
            stmt = _build_stmt(table, comment, column)
            try:
                run_sql(ws, stmt, warehouse_id=warehouse_id)
            except Exception as e:
                logger.warning("uc_comment write failed: %s", e)
                return {"status": "error", "statement": stmt, "message": str(e)}
            return {"status": "ok", "statement": stmt}

        # Bulk mode.
        # Validate every identifier up front — a malformed batch never
        # partially applies because of a bad name.
        for row in comments:
            err = _bad_ident(row.get("table"), row.get("column"))
            if err is not None:
                return {"status": "error", "applied": 0, "failed": len(comments),
                        "results": [{"status": "error", "message": err,
                                     "table": row.get("table"), "column": row.get("column")}]}

        results: list[dict[str, Any]] = []
        applied = 0
        for row in comments:
            r_table = row.get("table")
            r_column = row.get("column")
            r_comment = row.get("comment")
            stmt = _build_stmt(r_table, r_comment, r_column)
            try:
                run_sql(ws, stmt, warehouse_id=warehouse_id)
                applied += 1
                results.append({"status": "ok", "statement": stmt,
                                "table": r_table, "column": r_column})
            except Exception as e:
                logger.warning("uc_comment bulk write failed: %s", e)
                results.append({"status": "error", "statement": stmt, "message": str(e),
                                "table": r_table, "column": r_column})
        failed = len(results) - applied
        status = "ok" if failed == 0 else ("error" if applied == 0 else "partial")
        return {"status": status, "applied": applied, "failed": failed, "results": results}

    return build_tool(
        _update_uc_comment,
        name=name,
        description=_desc,
        resources=[ResourceSpec("sql_warehouse", warehouse_id)] if warehouse_id else (),
    )
