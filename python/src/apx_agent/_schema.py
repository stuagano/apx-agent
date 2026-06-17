"""Unity Catalog schema introspection + deterministic instruction generation.

Shared by ``DataAgent`` (to wire + ground an agent against a schema) and the
dev-UI setup wizard. Pure of any UI dependency.
"""

from __future__ import annotations

from typing import Any

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

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
        okf_root = d / APX_DIR / "okf"
        if okf_root.is_dir():
            try:
                from ._okf import okf_manifest

                parsed = okf_manifest(okf_root)  # totalised; None on any miss
            except Exception:
                parsed = None
            if parsed is not None:
                return parsed
            logger.warning(
                "OKF bundle at %s did not parse; falling back to schema.json cache.",
                okf_root,
            )
        candidate = d / APX_DIR / SCHEMA_MANIFEST_NAME
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text())
            except Exception:
                return None
            return data if isinstance(data, dict) else None
    return None


def load_okf_grounding(start: "Path | str | None" = None) -> "dict | None":
    """Harvest optional OKF enrichment for the first ``.apx/okf/`` found.

    Walks up from ``start`` (default cwd) like ``load_baked_schema``. Returns the
    per-table enrichment payload (see ``_okf.okf_grounding``) or ``None`` when no
    bundle is found or none carries enrichment. Totalised — never raises.
    """
    here = Path(start) if start is not None else Path.cwd()
    here = here.resolve()
    for d in [here, *here.parents]:
        okf_root = d / APX_DIR / "okf"
        if okf_root.is_dir():
            try:
                from ._okf import okf_grounding

                return okf_grounding(okf_root)
            except Exception:
                return None
    return None


def load_grounding_from_path(okf_root: "Path | str") -> "tuple[dict | None, dict | None]":
    """Load ``(manifest, grounding)`` directly from an explicit OKF bundle dir.

    Bypasses the cwd upward-walk — used by the ``knowledge =`` envelope knob.
    Returns ``(None, None)`` on any miss/error (totalised)."""
    from ._okf import okf_manifest, okf_grounding

    try:
        root = Path(okf_root)
        if not root.is_dir():
            return None, None
        return okf_manifest(root), okf_grounding(root)
    except Exception:
        return None, None


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


def introspect_schema_columns(
    ws: Any, catalog: str, schema: str
) -> dict[str, list[str]]:
    """Return ``{table_name: ["column(type)", ...]}`` via the Unity Catalog
    Tables API — no SQL warehouse required (unlike ``introspect_schema``).

    Used at scaffold time, where no warehouse is resolved. Best-effort: returns
    ``{}`` on any failure (no client, perms, network) so the scaffold proceeds
    without a manifest.
    """
    if not (ws and catalog and schema):
        return {}
    try:
        listed = list(ws.tables.list(catalog_name=catalog, schema_name=schema))
    except Exception:
        return {}
    result: dict[str, list[str]] = {}
    for t in listed:
        if not getattr(t, "name", None):
            continue
        cols = [
            f"{c.name}({c.type_text or ''})"
            for c in (getattr(t, "columns", None) or [])
            if getattr(c, "name", None)
        ]
        result[t.name] = cols
    return result


def _format_schema_block(
    tables: dict[str, list[str]], max_cols: int = 12, max_tables: int = 20
) -> str:
    """A bounded ``- table: col(type), ... (+N more)`` block for the prompt."""
    lines = []
    for name in list(tables.keys())[:max_tables]:
        cols = tables[name] or []
        shown = ", ".join(cols[:max_cols])
        if len(cols) > max_cols:
            shown += f" (+{len(cols) - max_cols} more)"
        lines.append(f"- {name}: {shown}" if shown else f"- {name}")
    if len(tables) > max_tables:
        lines.append(f"- (+{len(tables) - max_tables} more tables)")
    return "\n".join(lines)


def _format_grounded_schema_block(
    tables: dict[str, list[str]],
    grounding: dict,
    max_cols: int = 12,
    max_tables: int = 20,
) -> str:
    """Like ``_format_schema_block`` but appends per-table OKF enrichment.

    For a table with no enrichment entry the emitted line is byte-identical to
    ``_format_schema_block``'s line (F7 — every table is kept). Enriched tables
    gain indented description / column-descriptions / joins / one example, all
    bounded to mirror the plain block's caps.
    """
    lines: list[str] = []
    for name in list(tables.keys())[:max_tables]:
        cols = tables[name] or []
        shown = ", ".join(cols[:max_cols])
        if len(cols) > max_cols:
            shown += f" (+{len(cols) - max_cols} more)"
        lines.append(f"- {name}: {shown}" if shown else f"- {name}")
        enr = grounding.get(name) if grounding else None
        if not enr:
            continue
        if enr.get("description"):
            lines.append(f"    {enr['description'].splitlines()[0]}")
        described = [c for c in enr.get("columns", []) if c.get("description")][:max_cols]
        for c in described:
            lines.append(f"    - {c['name']}: {c['description']}")
        if enr.get("joins"):
            lines.append(f"    Joins: {enr['joins'].splitlines()[0]}")
        if enr.get("examples"):
            ex_lines = enr["examples"].strip().splitlines()[:6]
            lines.append("    Example:")
            lines.extend(f"      {l}" for l in ex_lines)
    if len(tables) > max_tables:
        lines.append(f"- (+{len(tables) - max_tables} more tables)")
    return "\n".join(lines)


def build_instructions_from_schema(
    catalog: str,
    schema: str,
    tables: dict[str, list[str]],
    persona: str | None = None,
    objective: str | None = None,
    grounding: dict | None = None,
) -> str:
    """Build agent instructions from schema metadata without an LLM call.

    When tables (with columns) are known, the instructions LIST the schema and
    tell the agent to query directly — no discovery step. When no tables are
    known, the agent is told to discover the schema with the SQL tool first.

    ``persona`` sets the agent's role ("a payroll analyst").
    ``objective`` sets its mission ("surface mismatches between hours worked and paychecks issued").
    When both persona and objective are given the lead is:
    "You are {persona} designed to {objective}."
    """
    if persona and objective:
        lead = f"You are {persona} designed to {objective}. "
    elif persona:
        lead = f"You are {persona}. "
    elif objective:
        lead = f"Your objective: {objective}. "
    else:
        lead = ""
    fqn = f"{catalog}.{schema}" if catalog and schema else schema or catalog or "the data"
    table_names = list(tables.keys())

    if not table_names:
        # Ungrounded: nothing known — tell the agent to discover first.
        return (
            lead + f"You are a data assistant for {fqn}. Your data includes: {fqn}.\n\n"
            f"At the start of every session, call the SQL tool to confirm what "
            f"tables and columns are available before answering questions.\n\n"
            f"To answer data questions: use the SQL tool with a targeted SELECT "
            f"statement. For aggregations: use GROUP BY with the appropriate "
            f"metric column.\n\n"
            f"When a query returns empty results or an error, try a broader filter "
            f"or verify the column name exists in the schema before telling the "
            f"user you cannot help.\n\n"
            f"Always base your answers on tool results. Never estimate or fabricate "
            f"data values. If you cannot retrieve what was asked, say so clearly "
            f"and describe what you can provide."
        )

    if len(table_names) == 1:
        chain = (
            f"To answer questions about {table_names[0]}: query the table with "
            f"targeted filters and return the results directly."
        )
    else:
        chain = (
            f"To answer questions about {table_names[0]}: query it with the relevant "
            f"filters. For questions spanning multiple tables "
            f"(e.g. {' and '.join(table_names[:2])}): run separate queries then "
            f"combine the results."
        )

    _block = (
        _format_grounded_schema_block(tables, grounding)
        if grounding
        else _format_schema_block(tables)
    )
    return (
        lead + f"You are a data assistant for {fqn}. You already know the schema below — "
        f"query the relevant table directly with the SQL tool. Do NOT run "
        f"SHOW TABLES or DESCRIBE to discover the structure; it is given here.\n\n"
        f"Schema:\n{_block}\n\n"
        f"{chain}\n\n"
        f"When a query returns empty results or an error, try a broader filter or "
        f"verify the column name exists in the schema before telling the user you "
        f"cannot help.\n\n"
        f"Always base your answers on tool results. Never estimate or fabricate "
        f"data values. If you cannot retrieve what was asked, say so clearly and "
        f"describe what you can provide."
    )
