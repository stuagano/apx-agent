"""Field-description curation helpers for the dev UI (#292 phase A).

Assemble the per-column "current vs suggested" curation state for an agent's OKF
bundle, and write accepted descriptions back into the bundle. The suggestion
source is Unity Catalog COMMENTs (the same source ``apx-agent agents
pull-comments`` uses).

Writing accepted descriptions edits the LOCAL OKF bundle (authoring — same trust
model as editing ``agent_router.py``); it does NOT write to Unity Catalog, so it
needs no governed-write path.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ._okf import apply_uc_comments, okf_columns, okf_manifest
from ._schema import APX_DIR

logger = logging.getLogger(__name__)


def resolve_okf_root(start: "Path | str | None" = None) -> "Path | None":
    """First ``.apx/okf`` bundle directory walking up from ``start`` (cwd by
    default), or ``None`` when the project has no OKF bundle."""
    here = (Path(start) if start is not None else Path.cwd()).resolve()
    for d in [here, *here.parents]:
        okf_root = d / APX_DIR / "okf"
        if okf_root.is_dir():
            return okf_root
    return None


def fetch_uc_comments(ws: Any, catalog: str, schema: str) -> dict[str, dict[str, str]]:
    """``{table: {col: comment}}`` from Unity Catalog. Totalised — returns ``{}``
    on any failure, so the curation view degrades to current-only (no
    suggestions) rather than erroring."""
    out: dict[str, dict[str, str]] = {}
    try:
        for t in ws.tables.list(catalog_name=catalog, schema_name=schema):
            tname = getattr(t, "name", None)
            if not tname:
                continue
            cmap: dict[str, str] = {}
            for c in (getattr(t, "columns", None) or []):
                cname = getattr(c, "name", None)
                if cname:
                    cmap[cname] = getattr(c, "comment", None) or ""
            out[tname] = cmap
    except Exception as e:
        logger.warning("fetch_uc_comments failed for %s.%s: %s", catalog, schema, e)
        return {}
    return out


def build_column_curation(okf_root: "Path", ws: "Any | None") -> dict[str, Any]:
    """Assemble the curation state: ``{catalog, schema, tables: [{table,
    columns: [{column, type, current, suggested}]}]}``.

    ``suggested`` is the UC comment when it is non-empty and differs from the
    current OKF description (else ``""``). ``ws=None`` or a UC failure leaves all
    suggestions empty — the current descriptions still render.
    """
    manifest = okf_manifest(okf_root) or {}
    catalog = manifest.get("catalog", "")
    schema = manifest.get("schema", "")
    cols_by_table = okf_columns(okf_root)
    uc = fetch_uc_comments(ws, catalog, schema) if (ws and catalog and schema) else {}
    tables: list[dict[str, Any]] = []
    for table, rows in cols_by_table.items():
        uc_cols = uc.get(table, {})
        columns = [
            {
                "column": r["name"],
                "type": r["type"],
                "current": r["description"],
                "suggested": (
                    uc_cols.get(r["name"], "")
                    if uc_cols.get(r["name"], "") and uc_cols.get(r["name"]) != r["description"]
                    else ""
                ),
            }
            for r in rows
        ]
        tables.append({"table": table, "columns": columns})
    return {"catalog": catalog, "schema": schema, "tables": tables}


def apply_column_descriptions(okf_root: "Path", accepted: dict[str, dict[str, str]]) -> int:
    """Write accepted ``{table: {col: description}}`` into the OKF bundle's
    ``# Schema`` Description cells (overwriting). Returns the number of tables
    modified. Blank descriptions are no-ops (you reject by not accepting)."""
    return apply_uc_comments(okf_root, accepted, overwrite=True)
