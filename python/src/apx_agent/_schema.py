"""Unity Catalog schema introspection + deterministic instruction generation.

Shared by ``DataAgent`` (to wire + ground an agent against a schema) and the
dev-UI setup wizard. Pure of any UI dependency.
"""

from __future__ import annotations

from typing import Any

import json
from pathlib import Path

APX_DIR = ".apx"
SCHEMA_MANIFEST_NAME = "schema.json"


def load_baked_schema(start: "Path | str | None" = None) -> "dict | None":
    """Find and parse the baked schema manifest ``.apx/schema.json``.

    Walks up from ``start`` (default: current working directory) to the
    filesystem root, returning the first ``.apx/schema.json`` parsed as a dict
    (keys: ``catalog``, ``schema``, ``tables``). Returns ``None`` when no
    manifest is found or it cannot be parsed — callers degrade to the generic
    (ungrounded) path rather than crash.
    """
    here = Path(start) if start is not None else Path.cwd()
    here = here.resolve()
    for d in [here, *here.parents]:
        candidate = d / APX_DIR / SCHEMA_MANIFEST_NAME
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text())
            except Exception:
                return None
            return data if isinstance(data, dict) else None
    return None


def introspect_schema(
    ws: Any,
    catalog: str,
    schema: str,
    warehouse_id: str | None = None,
) -> dict[str, list[str]]:
    """Return ``{table_name: ["column(type)", ...]}`` for a UC schema.

    Queries ``information_schema.columns`` via the workspace client's SQL
    execution. Best-effort: returns ``{}`` on any failure (missing args, no
    warehouse, permission/network errors) so callers degrade gracefully rather
    than crash.
    """
    if not (ws and catalog and schema):
        return {}
    from databricks.sdk.service.sql import StatementParameterListItem

    try:
        resp = ws.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = :schema "
                "ORDER BY table_name, ordinal_position"
            ),
            parameters=[
                StatementParameterListItem(name="schema", value=schema, type="STRING")
            ],
            catalog=catalog,
            schema=schema,
        )
    except Exception:
        return {}
    if not resp.result or not resp.result.data_array:
        return {}
    col_names = [c.name for c in resp.manifest.schema.columns]
    result: dict[str, list[str]] = {}
    for row in resp.result.data_array:
        r = dict(zip(col_names, row))
        result.setdefault(r["table_name"], []).append(
            f"{r['column_name']}({r['data_type']})"
        )
    return result


def build_instructions_from_schema(
    catalog: str,
    schema: str,
    tables: dict[str, list[str]],
) -> str:
    """Build agent instructions from schema metadata without an LLM call.

    Produces a 5-part structure (persona, first-tool rule, call chains, recovery
    rule, grounding rule) deterministically from the table names in hand.
    """
    fqn = f"{catalog}.{schema}" if catalog and schema else schema or catalog or "the data"
    table_names = list(tables.keys())

    if table_names:
        table_list = ", ".join(table_names)
        if len(table_names) == 1:
            chain = (
                f"To answer questions about {table_names[0]}: "
                f"query the table with targeted filters and return the results directly."
            )
        else:
            chain = (
                f"To answer questions about {table_names[0]}: query it with the relevant filters. "
                f"For questions spanning multiple tables (e.g. {' and '.join(table_names[:2])}): "
                f"run separate queries then combine the results."
            )
    else:
        table_list = fqn
        chain = (
            "To answer data questions: use the SQL tool with a targeted SELECT statement. "
            "For aggregations: use GROUP BY with the appropriate metric column."
        )

    return (
        f"You are a data assistant for {fqn}. "
        f"Your data includes: {table_list}.\n\n"
        f"At the start of every session, call the SQL tool to confirm what tables and columns "
        f"are available before answering questions.\n\n"
        f"{chain}\n\n"
        f"When a query returns empty results or an error, try a broader filter or verify the "
        f"column name exists in the schema before telling the user you cannot help.\n\n"
        f"Always base your answers on tool results. "
        f"Never estimate or fabricate data values. "
        f"If you cannot retrieve what was asked, say so clearly and describe what you can provide."
    )
