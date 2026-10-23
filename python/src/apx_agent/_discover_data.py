"""User-scoped Unity Catalog table metadata for the workspace graph."""

from __future__ import annotations

import re
from typing import Any

_INVALID_IDENTIFIER = re.compile(r"[`;\x00-\x1f\x7f]")
_MAX_TABLES = 100


def _validate_identifier(value: str, label: str) -> str:
    stripped = value.strip()
    if not stripped or stripped != value or len(stripped) > 255:
        raise ValueError(f"{label} must be a non-empty Unity Catalog identifier")
    if _INVALID_IDENTIFIER.search(stripped):
        raise ValueError(f"{label} contains an unsupported character")
    return stripped


def _column_info(column: Any) -> dict[str, Any]:
    name = str(getattr(column, "name", None) or "")
    type_text = getattr(column, "type_text", None)
    if not type_text:
        type_name = getattr(column, "type_name", None)
        type_text = getattr(type_name, "value", None) or str(type_name or "")
    return {
        "name": name,
        "type": str(type_text).lower(),
        "nullable": getattr(column, "nullable", None),
        "comment": getattr(column, "comment", None),
    }


def _row_count(detail: Any) -> int | None:
    properties = getattr(detail, "properties", None) or {}
    raw = properties.get("numRows") or properties.get("spark.sql.statistics.numRows")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _view_dependencies(detail: Any) -> list[str]:
    deps = (
        getattr(getattr(detail, "view_dependencies", None), "dependencies", None) or []
    )
    return [
        d.table.table_full_name
        for d in deps
        if getattr(d, "table", None) is not None and d.table.table_full_name
    ]


def list_discover_tables(
    ws: Any,
    catalog: str,
    schema: str,
    *,
    limit: int = _MAX_TABLES,
) -> list[dict[str, Any]]:
    """List bounded table metadata visible to the caller."""

    _validate_identifier(catalog, "catalog")
    _validate_identifier(schema, "schema")
    if limit < 1 or limit > _MAX_TABLES:
        raise ValueError(f"limit must be between 1 and {_MAX_TABLES}")

    tables: list[dict[str, Any]] = []
    for table in list(ws.tables.list(catalog_name=catalog, schema_name=schema))[:limit]:
        name = str(getattr(table, "name", None) or "")
        if not name:
            continue
        full_name = str(
            getattr(table, "full_name", None) or f"{catalog}.{schema}.{name}"
        )
        detail = table
        try:
            detail = ws.tables.get(full_name)
        except Exception:
            # A list result is still useful when detail permissions are narrower.
            pass
        table_type = getattr(detail, "table_type", None)
        tables.append(
            {
                "name": name,
                "full_name": full_name,
                "table_type": getattr(table_type, "value", None)
                or str(table_type or ""),
                "comment": getattr(detail, "comment", None)
                or getattr(table, "comment", None),
                "columns": [
                    _column_info(c) for c in (getattr(detail, "columns", None) or [])
                ],
                "row_count": _row_count(detail),
                "view_dependencies": _view_dependencies(detail),
            }
        )
    return sorted(tables, key=lambda item: item["name"].lower())

