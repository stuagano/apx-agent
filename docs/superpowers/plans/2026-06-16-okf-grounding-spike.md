# OKF Grounding Substrate — Phase 1 Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `.apx/okf/` (an OKF v0.1 bundle) the grounding source of truth for apx-agent, returning a byte-identical `{catalog, schema, tables}` dict through the existing `load_baked_schema()` seam, and prove it on `python/payroll-coworker/` with zero downstream change.

**Architecture:** Vendor a tiny OKF reader/writer (`_okf.py`) — mirroring the OKF reference `OKFDocument.parse/serialize/validate` plus the `# Schema`-pipe-table→column parser the reference lacks. Teach `load_baked_schema()` to prefer an `.apx/okf/` bundle (totalised, None-on-error) and fall back to `.apx/schema.json` (now a derived cache). Add an `apx-agent agents migrate-to-okf` command. Fix the deploy-copy heredoc so `.apx/` ships to the App container. Phase 2 enrichment (`load_okf_grounding()` + `grounding=` param) is explicitly OUT OF SCOPE — gated separately.

**Tech Stack:** Python 3.11+, PyYAML (already a transitive dep, lazy-imported), Click (CLI), pytest. Tests run with `cd python && uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-06-16-okf-grounding-design.md` (§4, §5, §7, §9, §10).

---

## Task 0: Isolated worktree

**Why:** Concurrent repo activity has deleted `python/` mid-session before (see project memory + spec §9). All spike work happens in a git worktree off the design branch.

- [ ] **Step 1: Create the worktree**

```bash
cd /Users/stuart.gano/Documents/apx-agent
git worktree add ../apx-agent-okf-spike design/okf-grounding-substrate
cd ../apx-agent-okf-spike
```

- [ ] **Step 2: Verify the test harness runs (baseline green)**

Run: `cd python && uv run pytest tests/test_schema.py -q`
Expected: PASS (establishes the editable-venv test env works before any change).

---

## Task 1: Vendor `_okf.py` — `OKFDocument` parse/serialize/validate

**Files:**
- Create: `python/src/apx_agent/_okf.py`
- Test: `python/tests/test_okf.py`

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_okf.py
"""Tests for the vendored OKF v0.1 reader/writer (_okf.py)."""
from __future__ import annotations

from apx_agent._okf import OKFDocument, REQUIRED_FRONTMATTER_KEYS


class TestOKFDocument:
    def test_parse_roundtrip(self):
        text = (
            "---\n"
            "type: Unity Catalog Table\n"
            "title: pay_runs\n"
            "description: One row per employee per pay period.\n"
            "timestamp: '2026-06-16T00:00:00+00:00'\n"
            "---\n\n"
            "# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `run_id` | string |  |\n"
        )
        doc = OKFDocument.parse(text)
        assert doc.frontmatter["type"] == "Unity Catalog Table"
        assert doc.frontmatter["title"] == "pay_runs"
        assert doc.body.startswith("# Schema")
        # serialize -> parse is stable on frontmatter + body
        again = OKFDocument.parse(doc.serialize())
        assert again.frontmatter == doc.frontmatter
        assert again.body.strip() == doc.body.strip()

    def test_parse_no_frontmatter_is_tolerant(self):
        doc = OKFDocument.parse("# Just a body\nno frontmatter")
        assert doc.frontmatter == {}
        assert "Just a body" in doc.body

    def test_body_with_horizontal_rule_not_split_as_frontmatter(self):
        # A `| --- |` table separator in the body must NOT be mistaken for the
        # closing frontmatter delimiter.
        text = "---\ntype: X\ntitle: t\ndescription: d\ntimestamp: z\n---\n\n# Schema\n| --- |\n| `a` | int |\n"
        doc = OKFDocument.parse(text)
        assert doc.frontmatter["type"] == "X"
        assert "| `a` | int |" in doc.body

    def test_validate_requires_keys_emit_side(self):
        import pytest
        doc = OKFDocument(frontmatter={"type": "X"}, body="")
        with pytest.raises(ValueError):
            doc.validate()  # missing title/description/timestamp
        OKFDocument(
            frontmatter={"type": "X", "title": "t", "description": "d", "timestamp": "z"},
            body="",
        ).validate()  # no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_okf.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'apx_agent._okf'`.

- [ ] **Step 3: Write minimal implementation**

```python
# python/src/apx_agent/_okf.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_okf.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_okf.py python/tests/test_okf.py
git commit -m "feat(okf): vendor OKFDocument parse/serialize/validate"
```

---

## Task 2: `parse_schema_columns` — `# Schema` body → `["col(type)"]`

**Files:**
- Modify: `python/src/apx_agent/_okf.py`
- Test: `python/tests/test_okf.py`

- [ ] **Step 1: Write the failing test (append to test_okf.py)**

```python
class TestParseSchemaColumns:
    def test_pipe_table_extracts_col_and_type(self):
        from apx_agent._okf import parse_schema_columns
        body = (
            "# Schema\n"
            "| Column | Type | Description |\n"
            "| --- | --- | --- |\n"
            "| `run_id` | string |  |\n"
            "| `gross_pay` | decimal(6,2) | Gross pay. |\n"
            "| `tags` | array<string> |  |\n"
        )
        # F2: header row + `---` separator are dropped; nested-paren/angle types verbatim.
        assert parse_schema_columns(body) == [
            "run_id(string)",
            "gross_pay(decimal(6,2))",
            "tags(array<string>)",
        ]

    def test_fk_link_in_description_does_not_pollute_name(self):
        from apx_agent._okf import parse_schema_columns
        body = (
            "# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `employee_id` | string | FK -> [`employees`](/tables/employees.md) |\n"
        )
        assert parse_schema_columns(body) == ["employee_id(string)"]

    def test_missing_schema_section_returns_empty(self):
        from apx_agent._okf import parse_schema_columns
        assert parse_schema_columns("# Overview\nno schema here") == []

    def test_stops_at_next_heading(self):
        from apx_agent._okf import parse_schema_columns
        body = (
            "# Schema\n| Column | Type |\n| --- | --- |\n| `a` | int |\n"
            "# Joins\n| `not_a_col` | nope |\n"
        )
        assert parse_schema_columns(body) == ["a(int)"]

    def test_bullet_form_best_effort(self):
        from apx_agent._okf import parse_schema_columns
        body = "# Schema\n- `event_date` (STRING): the date\n"
        assert parse_schema_columns(body) == ["event_date(STRING)"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_okf.py::TestParseSchemaColumns -q`
Expected: FAIL with `ImportError: cannot import name 'parse_schema_columns'`.

- [ ] **Step 3: Write minimal implementation (append to _okf.py)**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_okf.py -q`
Expected: PASS (all `TestOKFDocument` + `TestParseSchemaColumns`).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_okf.py python/tests/test_okf.py
git commit -m "feat(okf): parse # Schema pipe table to col(type) strings"
```

---

## Task 3: `okf_manifest(okf_root)` — bundle dir → `{catalog, schema, tables}`

**Files:**
- Modify: `python/src/apx_agent/_okf.py`
- Test: `python/tests/test_okf.py`

- [ ] **Step 1: Write the failing test (append to test_okf.py)**

```python
class TestOKFManifest:
    def _write_bundle(self, root, *, tables_order):
        okf = root / ".apx" / "okf"
        (okf / "datasets").mkdir(parents=True)
        (okf / "tables").mkdir(parents=True)
        (okf / "datasets" / "payroll_demo.md").write_text(
            "---\ntype: Databricks Schema\ntitle: payroll_demo\n"
            "description: d\ncatalog: cat\nschema: payroll_demo\ntimestamp: z\n---\n\n# Tables\n"
        )
        for t, cols in tables_order:
            rows = "".join(f"| `{c}` | int |  |\n" for c in cols)
            (okf / "tables" / f"{t}.md").write_text(
                f"---\ntype: Unity Catalog Table\ntitle: {t}\ndescription: d\ntimestamp: z\n---\n\n"
                f"# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n{rows}"
            )
        return okf

    def test_parses_catalog_schema_tables(self, tmp_path):
        from apx_agent._okf import okf_manifest
        okf = self._write_bundle(tmp_path, tables_order=[("employees", ["a", "b"]), ("pay_runs", ["c"])])
        out = okf_manifest(okf)
        assert out["catalog"] == "cat"
        assert out["schema"] == "payroll_demo"
        assert out["tables"] == {"employees": ["a(int)", "b(int)"], "pay_runs": ["c(int)"]}

    def test_excludes_reserved_index_md_no_phantom_table(self, tmp_path):
        from apx_agent._okf import okf_manifest
        okf = self._write_bundle(tmp_path, tables_order=[("employees", ["a"])])
        (okf / "tables" / "index.md").write_text("# Tables\n* [employees](employees.md)\n")
        out = okf_manifest(okf)
        assert "index" not in out["tables"]  # F3: reserved file is not a table
        assert set(out["tables"]) == {"employees"}

    def test_missing_dataset_returns_none(self, tmp_path):
        from apx_agent._okf import okf_manifest
        okf = tmp_path / ".apx" / "okf" / "tables"
        okf.mkdir(parents=True)
        assert okf_manifest(tmp_path / ".apx" / "okf") is None

    def test_malformed_bundle_returns_none_never_raises(self, tmp_path):
        from apx_agent._okf import okf_manifest
        okf = tmp_path / ".apx" / "okf"
        (okf / "datasets").mkdir(parents=True)
        (okf / "datasets" / "x.md").write_text("---\nnot: : valid: yaml\n: -\n---\n")
        assert okf_manifest(okf) is None  # F1: totality
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_okf.py::TestOKFManifest -q`
Expected: FAIL with `ImportError: cannot import name 'okf_manifest'`.

- [ ] **Step 3: Write minimal implementation (append to _okf.py)**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_okf.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_okf.py python/tests/test_okf.py
git commit -m "feat(okf): okf_manifest reader (reserved-file + totality guards)"
```

---

## Task 4: `write_okf_bundle` — manifest → bundle (with round-trip proof)

**Files:**
- Modify: `python/src/apx_agent/_okf.py`
- Test: `python/tests/test_okf.py`

- [ ] **Step 1: Write the failing test (append to test_okf.py)**

```python
class TestWriteOKFBundle:
    def test_roundtrip_manifest(self, tmp_path):
        from apx_agent._okf import write_okf_bundle, okf_manifest
        manifest = {
            "catalog": "serverless_stable_qh44kx_catalog",
            "schema": "payroll_demo",
            "tables": {
                "employees": ["employee_id(string)", "hire_date(date)"],
                "pay_runs": ["gross_pay(decimal(6,2))", "tags(array<string>)"],
            },
        }
        okf = tmp_path / ".apx" / "okf"
        write_okf_bundle(manifest, okf, timestamp="2026-06-16T00:00:00+00:00")
        assert okf_manifest(okf) == manifest  # exact round-trip incl. nested-paren/angle types

    def test_emitted_concepts_are_okf_conformant(self, tmp_path):
        from apx_agent._okf import write_okf_bundle, OKFDocument, REQUIRED_FRONTMATTER_KEYS
        write_okf_bundle(
            {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}},
            tmp_path, timestamp="z",
        )
        for md in (tmp_path / "tables").glob("*.md"):
            if md.name in {"index.md", "log.md"}:
                continue
            fm = OKFDocument.parse(md.read_text()).frontmatter
            assert all(fm.get(k) for k in REQUIRED_FRONTMATTER_KEYS)  # emit-side conformance

    def test_pipe_in_comment_is_escaped(self, tmp_path):
        from apx_agent._okf import write_okf_bundle, okf_manifest
        # A future column comment containing '|' must not shift the Type cell (F9).
        m = {"catalog": "c", "schema": "s", "tables": {"t": ["x(string)"]}}
        write_okf_bundle(m, tmp_path, timestamp="z", descriptions={"t": {"x": "a | b"}})
        assert okf_manifest(tmp_path)["tables"]["t"] == ["x(string)"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_okf.py::TestWriteOKFBundle -q`
Expected: FAIL with `ImportError: cannot import name 'write_okf_bundle'`.

- [ ] **Step 3: Write minimal implementation (append to _okf.py)**

```python
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

    # tables/index.md pins load-bearing order (matters when introspect order != alphabetical).
    (root / "tables" / "index.md").write_text(
        "# Tables\n" + "".join(f"* [{t}]({t}.md)\n" for t in tables)
    )
    # Bundle-root index.md is the ONLY place okf_version is declared (§11).
    (root / "index.md").write_text(
        f'---\nokf_version: "{OKF_VERSION}"\n---\n\n'
        "# Subdirectories\n* [datasets](datasets/)\n* [tables](tables/)\n"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_okf.py -q`
Expected: PASS (round-trip incl. `decimal(6,2)` and `array<string>`).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_okf.py python/tests/test_okf.py
git commit -m "feat(okf): write_okf_bundle emitter with round-trip + pipe-escape"
```

---

## Task 5: Branch `load_baked_schema` to prefer OKF (totalised, dual-read)

**Files:**
- Modify: `python/src/apx_agent/_schema.py:18-37` (`load_baked_schema`)
- Test: `python/tests/test_schema.py`

- [ ] **Step 1: Write the failing test (append to test_schema.py)**

```python
class TestLoadBakedSchemaOKF:
    def _write_okf(self, root, manifest):
        from apx_agent._okf import write_okf_bundle
        write_okf_bundle(manifest, root / "okf_tmp", timestamp="z")
        # move into .apx/okf
        import shutil
        dest = root / ".apx" / "okf"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root / "okf_tmp"), str(dest))

    def test_prefers_okf_bundle(self, tmp_path):
        m = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        self._write_okf(tmp_path, m)
        assert load_baked_schema(tmp_path) == m

    def test_okf_wins_over_stale_schema_json(self, tmp_path):
        stale = {"catalog": "c", "schema": "s", "tables": {"old": ["x(int)"]}}
        _write_manifest(tmp_path, stale)  # writes .apx/schema.json
        fresh = {"catalog": "c", "schema": "s", "tables": {"new": ["y(int)"]}}
        self._write_okf(tmp_path, fresh)
        assert load_baked_schema(tmp_path) == fresh  # OKF is source of truth

    def test_falls_back_to_schema_json_when_okf_malformed(self, tmp_path):
        cache = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        _write_manifest(tmp_path, cache)
        okf = tmp_path / ".apx" / "okf" / "datasets"
        okf.mkdir(parents=True)
        (okf / "x.md").write_text("---\n: bad: yaml\n---\n")  # parses to None
        assert load_baked_schema(tmp_path) == cache  # dual-read fallback, no crash
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_schema.py::TestLoadBakedSchemaOKF -q`
Expected: FAIL (OKF bundle ignored — current loader only reads `schema.json`).

- [ ] **Step 3: Replace `load_baked_schema` body in `_schema.py`**

Replace the loop in `load_baked_schema` (currently lines 29–37) so each level prefers `.apx/okf/`, then falls back to `.apx/schema.json`:

```python
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
            # OKF parse-miss -> fall through to schema.json at the SAME level
        candidate = d / APX_DIR / SCHEMA_MANIFEST_NAME
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text())
            except Exception:
                return None
            return data if isinstance(data, dict) else None
    return None
```

- [ ] **Step 4: Run tests (new + existing must all pass)**

Run: `cd python && uv run pytest tests/test_schema.py -q`
Expected: PASS — both `TestLoadBakedSchemaOKF` AND the pre-existing `TestLoadBakedSchema` (no `.apx/okf/` present → identical behavior).

- [ ] **Step 5: Run the broader grounding suite for regressions**

Run: `cd python && uv run pytest tests/test_schema.py tests/test_data_agent.py tests/test_coworker.py -q`
Expected: PASS (no downstream caller behavior change).

- [ ] **Step 6: Commit**

```bash
git add python/src/apx_agent/_schema.py python/tests/test_schema.py
git commit -m "feat(schema): load_baked_schema prefers .apx/okf bundle (dual-read, totalised)"
```

---

## Task 6: `apx-agent agents migrate-to-okf` command

**Files:**
- Modify: `python/src/apx_agent/cli.py` (add command near `refresh-schema` at `:2486`; import `APX_DIR`)
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: Write the failing test (append to test_cli.py)**

```python
class TestMigrateToOKF:
    def test_migrate_creates_bundle_and_regenerates_cache(self, tmp_path, monkeypatch):
        import json
        from click.testing import CliRunner
        from apx_agent.cli import agents

        apx = tmp_path / ".apx"
        apx.mkdir()
        manifest = {
            "catalog": "serverless_stable_qh44kx_catalog",
            "schema": "payroll_demo",
            "tables": {"employees": ["employee_id(string)"], "pay_runs": ["gross_pay(decimal(6,2))"]},
        }
        (apx / "schema.json").write_text(json.dumps(manifest))
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(agents, ["migrate-to-okf"])
        assert result.exit_code == 0, result.output
        assert (apx / "okf" / "datasets" / "payroll_demo.md").is_file()
        assert (apx / "okf" / "tables" / "pay_runs.md").is_file()

        from apx_agent._schema import load_baked_schema
        assert load_baked_schema(tmp_path) == manifest  # OKF round-trips the original

    def test_refuses_existing_bundle_without_force(self, tmp_path, monkeypatch):
        import json
        from click.testing import CliRunner
        from apx_agent.cli import agents

        apx = tmp_path / ".apx"
        (apx / "okf").mkdir(parents=True)
        (apx / "schema.json").write_text(json.dumps({"catalog": "c", "schema": "s", "tables": {}}))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(agents, ["migrate-to-okf"])
        assert result.exit_code != 0
        assert "force" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_cli.py::TestMigrateToOKF -q`
Expected: FAIL — `No such command 'migrate-to-okf'`.

- [ ] **Step 3: Add the command in `cli.py`**

Ensure `APX_DIR` is imported (the file already has `from ._schema import introspect_schema_columns` at `:55` — extend it):

```python
from ._schema import introspect_schema_columns, APX_DIR
```

Add the command next to `refresh-schema` (after the function ending near `:2486`):

```python
@agents.command("migrate-to-okf")
@click.option("--force", is_flag=True, help="Overwrite an existing .apx/okf bundle.")
def migrate_to_okf(force: bool) -> None:
    """Convert this project's .apx/schema.json into an .apx/okf/ bundle.

    Reads the existing manifest, emits an OKF v0.1 bundle (the new source of
    truth), then regenerates .apx/schema.json as the derived cache. Idempotent;
    refuses to clobber an existing bundle without --force.
    """
    from datetime import datetime, timezone
    from ._okf import write_okf_bundle, okf_manifest

    apx = Path.cwd() / APX_DIR
    manifest_path = apx / "schema.json"
    okf_root = apx / "okf"
    if not manifest_path.is_file():
        raise click.ClickException(
            "No .apx/schema.json found. Run inside a scaffolded project."
        )
    if okf_root.exists() and not force:
        raise click.ClickException(".apx/okf already exists. Use --force to overwrite.")
    manifest = _json.loads(manifest_path.read_text())
    ts = datetime.now(timezone.utc).isoformat()
    write_okf_bundle(manifest, okf_root, timestamp=ts)
    regen = okf_manifest(okf_root)
    if regen is not None:
        manifest_path.write_text(_json.dumps(regen, indent=2))
    click.echo(f"Wrote OKF bundle to {okf_root} (schema.json regenerated as derived cache).")
```

> Note: `_json` is the module's existing `import json as _json` alias (used at `cli.py:1879`). If absent in scope, use `json`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_cli.py::TestMigrateToOKF -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_cli.py
git commit -m "feat(cli): add 'agents migrate-to-okf' command"
```

---

## Task 7: Fix the deploy-copy heredoc so `.apx/` ships (F8)

**Files:**
- Modify: `python/src/apx_agent/cli.py:1113` (the `artifacts.default.build` heredoc)
- Test: `python/tests/test_cli_scaffold_yaml.py`

- [ ] **Step 1: Write the failing test**

First find the scaffold-output assertion helper in `test_cli_scaffold_yaml.py` (it renders the `databricks.yml`/build template). Add:

```python
def test_build_heredoc_copies_apx_dir():
    # The OKF bundle (and the derived schema.json cache) live under .apx/ and
    # MUST ship to the App container, else the deployed agent is ungrounded (F8).
    from apx_agent import cli
    import inspect

    src = inspect.getsource(cli)
    # The build heredoc copies .apx-agent today; it must also copy .apx.
    assert "cp -r .apx .build/" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_cli_scaffold_yaml.py::test_build_heredoc_copies_apx_dir -q`
Expected: FAIL (`cp -r .apx .build/` not present — only `cp -r .apx-agent .build/`).

- [ ] **Step 3: Add the copy line to the heredoc**

In the `artifacts.default.build` template string (currently `cli.py:1106–1114`), add a line immediately after the `.apx-agent` copy (line 1113):

```
      cp -r .apx-agent .build/ 2>/dev/null || true
      cp -r .apx .build/ 2>/dev/null || true
      cp apx_agent-*.whl .build/ 2>/dev/null || true
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_cli_scaffold_yaml.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_cli_scaffold_yaml.py
git commit -m "fix(cli): ship .apx/ (OKF bundle + cache) to the App container (F8)"
```

---

## Task 8: Spike execution + equivalence gate on payroll-coworker

**Files:**
- Create: `python/tests/test_okf_equivalence.py`
- Generate (committed artifact): `python/payroll-coworker/.apx/okf/**`

This is the spike's proof. It must EXECUTE and be INSPECTED (spec §9 exit criteria) — not just asserted in a unit test.

- [ ] **Step 1: Write the equivalence test using the REAL payroll manifest**

```python
# python/tests/test_okf_equivalence.py
"""Phase-1 transparency proof: an OKF bundle round-trips payroll-coworker's
real .apx/schema.json with byte-identical grounding outputs."""
from __future__ import annotations

import json
from pathlib import Path

from apx_agent._okf import write_okf_bundle, okf_manifest
from apx_agent._schema import build_instructions_from_schema

REAL_MANIFEST = (
    Path(__file__).resolve().parents[2] / "payroll-coworker" / ".apx" / "schema.json"
)


def _load_real():
    return json.loads(REAL_MANIFEST.read_text())


def test_dict_equality_order_insensitive(tmp_path):
    m = _load_real()
    write_okf_bundle(m, tmp_path / "okf", timestamp="2026-06-16T00:00:00+00:00")
    out = okf_manifest(tmp_path / "okf")
    assert out is not None
    assert out["catalog"] == m["catalog"]
    assert out["schema"] == m["schema"]
    assert out["tables"] == m["tables"]  # all 5 tables, exact col(type) strings


def test_prompt_string_identity(tmp_path):
    m = _load_real()
    write_okf_bundle(m, tmp_path / "okf", timestamp="2026-06-16T00:00:00+00:00")
    okf_tables = okf_manifest(tmp_path / "okf")["tables"]
    before = build_instructions_from_schema(m["catalog"], m["schema"], m["tables"])
    after = build_instructions_from_schema(m["catalog"], m["schema"], okf_tables)
    assert after == before  # byte-identical (order-sensitive _format_schema_block)
```

- [ ] **Step 2: Run it to verify it passes (proves dict + prompt equivalence)**

Run: `cd python && uv run pytest tests/test_okf_equivalence.py -q`
Expected: PASS. If `test_prompt_string_identity` fails, the table order diverged — confirm `tables/index.md` ordering matches the manifest's key order.

- [ ] **Step 3: Actually run the migration on the real project (EXECUTE)**

```bash
cd ../apx-agent-okf-spike/python/payroll-coworker
uv run apx-agent agents migrate-to-okf
```

Expected output: `Wrote OKF bundle to .../.apx/okf (schema.json regenerated as derived cache).`

- [ ] **Step 4: Inspect the generated bundle (INSPECT — do not skip)**

```bash
find .apx/okf -type f | sort
cat .apx/okf/tables/pay_runs.md
git --no-pager diff --stat .apx/schema.json
```

Expected: `datasets/payroll_demo.md`, `tables/{agent_memory,apx_payroll_coworker_memory,apx_payroll_coworker_sessions,employees,pay_runs}.md`, `tables/index.md`, `index.md`. `pay_runs.md` shows a `# Schema` pipe table with `gross_pay | decimal(6,2)`. The `schema.json` diff is empty or whitespace-only (cache regenerated identically).

- [ ] **Step 5: Prove the resource set is unchanged (EXECUTE)**

Confirm the five `uc_table` ResourceSpecs build identically from the OKF-backed loader. Run from `python/`:

```bash
cd ../apx-agent-okf-spike/python
uv run python - <<'PY'
from apx_agent._schema import load_baked_schema
m = load_baked_schema("payroll-coworker")
assert m is not None, "loader returned None"
print("catalog:", m["catalog"], "schema:", m["schema"])
print("tables:", sorted(m["tables"]))
assert sorted(m["tables"]) == [
    "agent_memory", "apx_payroll_coworker_memory",
    "apx_payroll_coworker_sessions", "employees", "pay_runs",
], "table set changed — uc_table ResourceSpec set would differ"
print("OK: 5 tables, uc_table resource set preserved")
PY
```

Expected: prints the catalog/schema and `OK: 5 tables...`. (The loader now reads the OKF bundle, since `.apx/okf/` exists after Step 3.)

- [ ] **Step 6: Full suite green**

Run: `cd ../apx-agent-okf-spike/python && uv run pytest -q`
Expected: PASS (no regressions across the suite).

- [ ] **Step 7: Commit the spike artifact + test**

```bash
git add python/tests/test_okf_equivalence.py python/payroll-coworker/.apx
git commit -m "test(okf): payroll-coworker equivalence gate + migrated OKF bundle"
```

---

## Out of scope (gated separately — do NOT build here)

Per spec §6 and the locked decisions, these are explicitly deferred:
- `load_okf_grounding()` accessor + `grounding=` param on `build_instructions_from_schema` (Phase-2 enrichment reaching the prompt).
- `_format_grounded_schema_block` and the single `data_agent.py:132` pass-through.
- Scaffold emitting OKF for NEW projects (`cli.py:1879`/`:2038`).
- `refresh-schema` re-introspecting only the `# Schema` section (spec Q1).
- Pre-commit cache-regen hook (spec Q2).

---

## Self-review notes

- **Spec coverage:** §4 loader seam → Task 5; §5 converter both directions → Tasks 2,4; §7 migration/back-compat → Tasks 5,6; F8 deploy-copy → Task 7; §9 spike exit criteria (a)+(b)+(c) → Task 8 Steps 2,5; §10 totality/F2/F3/F6/F9 tests → Tasks 2,3,4.
- **F1 totality** asserted in Task 3 Step 1 (`test_malformed_bundle_returns_none_never_raises`) and Task 5 Step 1 (`test_falls_back_to_schema_json_when_okf_malformed`).
- **Type/name consistency:** `okf_manifest`, `write_okf_bundle(…, *, timestamp, descriptions=None)`, `parse_schema_columns`, `OKFDocument`, `_split_col`, `_RESERVED` used identically across tasks.
