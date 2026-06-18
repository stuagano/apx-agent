"""Vendored OKF v0.1 (Draft) reader/writer — apx's grounding substrate.

Mirrors the OKF reference implementation (GoogleCloudPlatform/knowledge-catalog
/okf, Apache-2.0) ``OKFDocument.parse/serialize/validate`` and adds the
``# Schema`` pipe-table -> ``"col(type)"`` parser the reference lacks. Pinned to
OKF SPEC v0.1 §4. Re-check on ``okf_version`` bumps.

Totality contract: every reader here returns ``None``/``[]`` on bad input and
NEVER raises out to callers (mirrors ``load_baked_schema``'s None-on-error). The
only function that raises is ``validate()``, which is EMIT-side only and MUST NOT
be called on the read path (spec §3, F5).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import logging
import re

logger = logging.getLogger(__name__)

REQUIRED_FRONTMATTER_KEYS = ("type", "title", "description", "timestamp")
OKF_VERSION = "0.1"


def dump_schema_cache(manifest: dict) -> str:
    """Canonical serialization of the derived ``schema.json`` cache.

    The single writer shared by the CLI lifecycle commands (scaffold,
    refresh-schema, migrate-to-okf) and the pre-commit regen hook, so the two
    never emit byte-different caches that rewrite each other on every run (no
    flip-flop). ``indent=2``, no trailing newline — matches the committed
    caches already in the tree, so adopting it churns nothing.
    """
    return json.dumps(manifest, indent=2)


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass
class OKFDocument:
    frontmatter: dict
    body: str

    @classmethod
    def parse(cls, text: str) -> "OKFDocument":
        m = _FM_RE.match(text)
        if m:
            import yaml

            fm = yaml.safe_load(m.group(1)) or {}
            return cls(
                frontmatter=fm if isinstance(fm, dict) else {},
                body=m.group(2).lstrip("\n"),
            )
        return cls(frontmatter={}, body=text)

    def serialize(self) -> str:
        import yaml

        fm = yaml.safe_dump(self.frontmatter, sort_keys=False).strip()
        return f"---\n{fm}\n---\n\n{self.body}"

    def validate(self) -> None:
        """Emit-side conformance gate. NEVER call on the read path (F5)."""
        for k in REQUIRED_FRONTMATTER_KEYS:
            if not self.frontmatter.get(k):
                raise ValueError(f"OKF concept missing required frontmatter key: {k!r}")


_BACKTICK_IDENT = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")


def _extract_section(body: str, heading: str) -> str:
    """Lines under ``# <heading>`` up to the next top-level ``# `` heading."""
    out: list[str] = []
    capturing = False
    for line in body.splitlines():
        if re.match(r"^#\s+", line):
            if capturing:
                break
            capturing = re.match(rf"^#\s+{re.escape(heading)}\s*$", line) is not None
            continue
        if capturing:
            out.append(line)
    return "\n".join(out)


def parse_schema_columns(body: str) -> list[str]:
    """Extract ``["col(type)", ...]`` from a concept body's ``# Schema`` section.

    Handles the SPEC §4.2 pipe table (apx's emit form) and, best-effort, the
    bullet form. Returns ``[]`` when no ``# Schema`` section is present or no
    data rows parse — never raises, never emits header/separator rows (F2/F6).
    """
    section = _extract_section(body, "Schema")
    if not section:
        return []
    cols: list[str] = []
    for raw in section.splitlines():
        line = raw.strip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            name_m = _BACKTICK_IDENT.search(cells[0]) if cells else None
            if not name_m:  # F2: drops the "| Column |" header and "| --- |" separator
                continue
            type_text = cells[1].strip() if len(cells) > 1 else ""
            cols.append(f"{name_m.group(1)}({type_text})")
        elif line.startswith(("-", "*")):
            name_m = _BACKTICK_IDENT.search(line)
            if not name_m:
                continue
            tm = re.search(r"\(([^)]*)\)", line[name_m.end():])
            cols.append(f"{name_m.group(1)}({tm.group(1).strip() if tm else ''})")
    return cols


_RESERVED = {"index.md", "log.md"}


def _index_order(index_text: str) -> list[str]:
    """Stems listed (in order) by an ``index.md`` ``* [title](stem.md)`` body."""
    return [m.group(1) for m in re.finditer(r"\]\(([^)]+?)\.md\)", index_text)]


def _ordered_table_files(okf_root: Path) -> list[Path]:
    tdir = okf_root / "tables"
    if not tdir.is_dir():
        return []
    files = sorted(p for p in tdir.glob("*.md") if p.name not in _RESERVED)  # F3 primary order
    idx = tdir / "index.md"
    if idx.is_file():
        bystem = {p.stem: p for p in files}
        order = [bystem[s] for s in _index_order(idx.read_text()) if s in bystem]  # advisory
        listed = {p.stem for p in order}
        order += [p for p in files if p.stem not in listed]
        if order:
            return order
    return files


def _dataset_concept(okf_root: Path) -> "OKFDocument | None":
    ds_dir = okf_root / "datasets"
    if not ds_dir.is_dir():
        return None
    files = sorted(p for p in ds_dir.glob("*.md") if p.name not in _RESERVED)
    if not files:
        return None
    return OKFDocument.parse(files[0].read_text())  # F10: deterministic first


def okf_manifest(okf_root: "Path | str") -> "dict | None":
    """Parse an OKF bundle directory into ``{catalog, schema, tables}``.

    Returns ``None`` on ANY miss/error (no dataset concept, missing catalog/
    schema, unreadable files, bad YAML). NEVER raises — totality is the
    contract that keeps ``load_baked_schema`` crash-free (F1).
    """
    try:
        root = Path(okf_root)
        ds = _dataset_concept(root)
        if ds is None:
            return None
        catalog = ds.frontmatter.get("catalog")
        schema = ds.frontmatter.get("schema")
        if not catalog or not schema:
            return None
        tables: dict[str, list[str]] = {}
        for table_md in _ordered_table_files(root):
            doc = OKFDocument.parse(table_md.read_text())
            name = doc.frontmatter.get("title") or table_md.stem
            tables[name] = parse_schema_columns(doc.body)  # [] when no # Schema (name still kept)
        return {"catalog": catalog, "schema": schema, "tables": tables}
    except Exception:
        return None


def _split_col(col: str) -> "tuple[str, str]":
    """``'gross_pay(decimal(6,2))'`` -> ``('gross_pay', 'decimal(6,2)')``.

    Splits on the FIRST ``(`` (col names never contain ``(``) and the LAST ``)``
    so nested-paren types survive.
    """
    i = col.find("(")
    if i == -1:
        return col, ""
    j = col.rfind(")")
    return col[:i], (col[i + 1:j] if j > i else col[i + 1:])


def _schema_row(col: str, comment: str = "") -> str:
    name, type_text = _split_col(col)
    type_text = type_text.replace("|", r"\|")  # F9
    comment = (comment or "").replace("\n", " ").replace("|", r"\|")
    return f"| `{name}` | {type_text} | {comment} |\n"


def write_okf_bundle(
    manifest: dict,
    okf_root: "Path | str",
    *,
    timestamp: str,
    descriptions: "dict | None" = None,
) -> None:
    """Emit an OKF v0.1 bundle from a ``{catalog, schema, tables}`` manifest.

    ``descriptions`` is an optional ``{table: {col: comment}}`` map (Phase-2
    enrichment seed; comments are blank in Phase 1). Every concept is validated
    emit-side and carries all REQUIRED_FRONTMATTER_KEYS.
    """
    root = Path(okf_root)
    (root / "datasets").mkdir(parents=True, exist_ok=True)
    (root / "tables").mkdir(parents=True, exist_ok=True)
    catalog, schema = manifest["catalog"], manifest["schema"]
    tables = manifest.get("tables", {})
    descriptions = descriptions or {}

    ds = OKFDocument(
        frontmatter={
            "type": "Databricks Schema",
            "title": schema,
            "description": f"{schema} schema for the agent.",
            "resource": f"{catalog}.{schema}",
            "catalog": catalog,
            "schema": schema,
            "timestamp": timestamp,
        },
        body="# Tables\n" + "".join(f"* [{t}](../tables/{t}.md)\n" for t in tables),
    )
    ds.validate()
    (root / "datasets" / f"{schema}.md").write_text(ds.serialize())

    for t, cols in tables.items():
        col_comments = descriptions.get(t, {})
        rows = "".join(_schema_row(c, col_comments.get(_split_col(c)[0], "")) for c in cols)
        doc = OKFDocument(
            frontmatter={
                "type": "Unity Catalog Table",
                "title": t,
                "description": f"{t} table.",
                "resource": f"{catalog}.{schema}.{t}",
                "timestamp": timestamp,
            },
            body="# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n" + rows,
        )
        doc.validate()
        (root / "tables" / f"{t}.md").write_text(doc.serialize())

    (root / "tables" / "index.md").write_text(
        "# Tables\n" + "".join(f"* [{t}]({t}.md)\n" for t in tables)
    )
    (root / "index.md").write_text(
        f'---\nokf_version: "{OKF_VERSION}"\n---\n\n'
        "# Subdirectories\n* [datasets](datasets/)\n* [tables](tables/)\n"
    )


def _schema_rows_with_desc(body: str) -> list[dict]:
    """Per-column {name, type, description} from a ``# Schema`` pipe table."""
    section = _extract_section(body, "Schema")
    rows: list[dict] = []
    for raw in section.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        name_m = _BACKTICK_IDENT.search(cells[0]) if cells else None
        if not name_m:
            continue
        rows.append({
            "name": name_m.group(1),
            "type": cells[1].strip() if len(cells) > 1 else "",
            "description": cells[2].strip() if len(cells) > 2 else "",
        })
    return rows


def _first_code_block(section: str) -> str:
    """Body of the first fenced ``` block in a section, or ''."""
    m = re.search(r"```[a-zA-Z]*\n(.*?)```", section, re.DOTALL)
    return m.group(1).strip() if m else ""


def okf_grounding(okf_root: "Path | str") -> "dict | None":
    """Harvest optional per-table enrichment from an OKF bundle's bodies.

    Returns ``None`` when no table carries enrichment beyond bare auto-gen, so
    un-enriched bundles produce a byte-identical prompt. Totalised — never
    raises (mirrors ``okf_manifest``).
    """
    try:
        root = Path(okf_root)
        out: dict[str, dict] = {}
        for table_md in _ordered_table_files(root):
            doc = OKFDocument.parse(table_md.read_text())
            name = doc.frontmatter.get("title") or table_md.stem
            body = doc.body
            overview = _extract_section(body, "Overview").strip()
            joins = _extract_section(body, "Joins").strip()
            examples_sec = _extract_section(body, "Examples")
            examples = _first_code_block(examples_sec) if examples_sec.strip() else ""
            columns = _schema_rows_with_desc(body)
            has_col_desc = any(c["description"] for c in columns)
            if overview or joins or examples or has_col_desc:
                out[name] = {
                    "description": overview,
                    "columns": columns,
                    "joins": joins,
                    "examples": examples,
                }
        return out or None
    except Exception:
        return None


def _replace_section(body: str, heading: str, new_block: str) -> str:
    """Replace the ``# <heading>`` section (its heading line through just before
    the next top-level ``# `` heading) with ``new_block`` (which includes its own
    ``# <heading>`` line). Appends ``new_block`` when the section is absent."""
    lines = body.splitlines()
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if re.match(rf"^#\s+{re.escape(heading)}\s*$", line):
            start = i
            for j in range(i + 1, len(lines)):
                if re.match(r"^#\s+", lines[j]):
                    end = j
                    break
            break
    new_lines = new_block.rstrip("\n").splitlines()
    if start is None:
        return body.rstrip("\n") + "\n\n" + "\n".join(new_lines) + "\n"
    rebuilt = lines[:start] + new_lines + [""] + lines[end:]
    return "\n".join(rebuilt).rstrip("\n") + "\n"


def _schema_block_md(cols: list[str], descriptions: "dict | None" = None) -> str:
    """A full ``# Schema`` pipe-table block for the given ``col(type)`` strings,
    carrying over ``descriptions`` ({col: text}) into the 3rd cell."""
    descriptions = descriptions or {}
    rows = "".join(_schema_row(c, descriptions.get(_split_col(c)[0], "")) for c in cols)
    return "# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n" + rows


def apply_uc_comments(
    okf_root: "Path | str",
    comments: dict,
    *,
    overwrite: bool = False,
) -> int:
    """Fill OKF table bodies from Unity Catalog comments (read-side enrichment).

    ``comments`` = ``{table: {"_table": <table comment>, <col>: <col comment>}}``.
    Fills empty ``# Schema`` Description cells and a missing ``# Overview`` from
    the table comment; with ``overwrite`` it replaces curated values too. Never
    changes column names/types (``okf_manifest`` is unaffected). Returns the
    number of table concepts modified. Totalised per-table: a bad/unknown table
    is skipped, never raises out."""
    root = Path(okf_root)
    tdir = root / "tables"
    modified = 0
    skipped = 0
    for table, cmap in comments.items():
        path = tdir / f"{table}.md"
        if not path.is_file():
            continue
        try:
            doc = OKFDocument.parse(path.read_text())
            rows = _schema_rows_with_desc(doc.body)
            new_desc: dict[str, str] = {}
            changed = False
            for r in rows:
                cur = r["description"]
                uc = cmap.get(r["name"], "")
                if uc and (overwrite or not cur):
                    new_desc[r["name"]] = uc
                    changed = changed or uc != cur
                else:
                    new_desc[r["name"]] = cur
            if changed:
                cols = [f"{r['name']}({r['type']})" for r in rows]
                doc.body = _replace_section(doc.body, "Schema", _schema_block_md(cols, new_desc))
            tcomment = cmap.get("_table", "")
            existing_overview = _extract_section(doc.body, "Overview").strip()
            if tcomment and (overwrite or not existing_overview):
                safe = re.sub(r"^(#+)", r"\\\1", tcomment, flags=re.M)
                doc.body = _replace_section(doc.body, "Overview", f"# Overview\n{safe}")
                changed = True
            if changed:
                path.write_text(doc.serialize())
                modified += 1
        except Exception as e:
            skipped += 1
            logger.warning("apply_uc_comments: skipped table %r: %s", table, e)
            continue
    if skipped:
        logger.warning(
            "apply_uc_comments: skipped %d malformed table%s",
            skipped, "" if skipped == 1 else "s",
        )
    return modified


def refresh_okf_schema(
    okf_root: "Path | str", manifest: dict, *, timestamp: str, prune: bool = False
) -> None:
    """Update an OKF bundle's ``# Schema`` tables to match ``manifest`` while
    preserving enriched bodies and per-column descriptions.

    NON-DESTRUCTIVE by default: only the ``# Schema`` section of each live table
    is rewritten (Overview/Joins/Examples and other hand-authored sections are
    kept), and table concepts present in the bundle but ABSENT from ``manifest``
    (local-only / hand-authored tables) are LEFT IN PLACE and still listed in the
    indexes. Pass ``prune=True`` to delete those dropped-table concepts — the
    explicit opt-in behind ``refresh-schema --prune-missing-tables``. Caller
    regenerates the ``schema.json`` cache afterwards."""
    root = Path(okf_root)
    catalog, schema = manifest["catalog"], manifest["schema"]
    tables = manifest.get("tables", {})
    tdir = root / "tables"
    tdir.mkdir(parents=True, exist_ok=True)

    for name, cols in tables.items():
        path = tdir / f"{name}.md"
        if path.is_file():
            doc = OKFDocument.parse(path.read_text())
            old_desc = {r["name"]: r["description"] for r in _schema_rows_with_desc(doc.body)}
            doc.frontmatter["timestamp"] = timestamp
            doc.body = _replace_section(doc.body, "Schema", _schema_block_md(cols, old_desc))
            path.write_text(doc.serialize())
        else:
            doc = OKFDocument(
                frontmatter={
                    "type": "Unity Catalog Table", "title": name,
                    "description": f"{name} table.",
                    "resource": f"{catalog}.{schema}.{name}", "timestamp": timestamp,
                },
                body=_schema_block_md(cols),
            )
            doc.validate()
            path.write_text(doc.serialize())

    # Table concepts on disk that the live schema no longer lists. With prune
    # they are deleted; by default they are preserved (local-only / hand-authored)
    # and kept in the indexes so nothing the user wrote silently vanishes.
    local_only: list[str] = []
    for p in sorted(tdir.glob("*.md")):
        if p.name in _RESERVED:
            continue
        title = OKFDocument.parse(p.read_text()).frontmatter.get("title") or p.stem
        if title not in tables:
            if prune:
                p.unlink()
            else:
                local_only.append(p.stem)

    listed = list(tables) + local_only
    ds_path = root / "datasets" / f"{schema}.md"
    if ds_path.is_file():
        ds = OKFDocument.parse(ds_path.read_text())
        ds.frontmatter["timestamp"] = timestamp
        ds.body = "# Tables\n" + "".join(f"* [{t}](../tables/{t}.md)\n" for t in listed)
        ds_path.write_text(ds.serialize())
    (tdir / "index.md").write_text("# Tables\n" + "".join(f"* [{t}]({t}.md)\n" for t in listed))
