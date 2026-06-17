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
import re

REQUIRED_FRONTMATTER_KEYS = ("type", "title", "description", "timestamp")
OKF_VERSION = "0.1"

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
