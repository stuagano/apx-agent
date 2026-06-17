# OKF Grounding Phase 2 — Enrichment Reaches the Prompt

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let human/agent enrichment in an OKF bundle's markdown bodies (`# Overview`, per-column descriptions, `# Joins`, `# Examples`) flow into a `DataAgent`'s system prompt — additively, and **byte-identical to Phase 1 when nothing is enriched**.

**Architecture:** A separate `load_okf_grounding()` accessor (mirroring `load_baked_schema`'s upward walk) harvests an optional enrichment payload from the OKF bodies via a new `okf_grounding()` reader in `_okf.py`. `build_instructions_from_schema` gains a default-`None` `grounding=` param; when present it renders via a new `_format_grounded_schema_block` that keeps **all** tables (F7) and appends enrichment per table; when `None`/empty it renders exactly today's `_format_schema_block`. `data_agent.py` wires `grounding=` **only when the tables came from the baked OKF path** — never from a `tables=` override or live introspection, and never through the `instructions=` override (which still wins, per spec Q3).

**Tech Stack:** Python 3.11+, PyYAML, Click, pytest. Tests: `cd python && uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-06-16-okf-grounding-design.md` §6 (and Q3: "user `instructions=` wins, full stop" — permanent contract). Builds on the Phase-1 spike (`_okf.py`, `load_baked_schema` OKF branch, `migrate-to-okf`).

**Out of scope (deferred):** scaffold emitting OKF for new projects (`cli.py:1879`/`:2038`); `refresh-schema` preserving enriched bodies (spec Q1); cache-regen pre-commit hook (spec Q2); auto-emitting empty `# Overview`/`# Joins`/`# Examples` stub headings. Authors add the headings by hand for now.

**Invariant that gates every task:** with no enrichment present anywhere in the bundle, `okf_grounding()` returns `None`, so the rendered prompt is **byte-identical** to Phase 1. Every existing test stays green without modification.

---

## Task 0: Worktree (already created by controller)

Worktree at `/Users/stuart.gano/Documents/apx-agent-okf-phase2` on branch `impl/okf-grounding-phase2` (off the Phase-1 tip). Verify baseline:

- [ ] **Step 1:** Run `cd python && uv run pytest tests/test_okf.py tests/test_schema.py -q` → expect green (Phase-1 tests pass on this branch).

---

## Task 1: `okf_grounding(okf_root)` — harvest enrichment from bodies

**Files:** Modify `python/src/apx_agent/_okf.py`; Test `python/tests/test_okf.py`.

Harvest, per table concept, an optional enrichment record. Return `None` when **no** table carries any enrichment beyond bare auto-gen (so un-enriched bundles render identically). Totalised (never raises). The payload shape:

```python
{ "<table>": {
    "description": "<# Overview prose, stripped, or ''>",
    "columns": [ {"name": str, "type": str, "description": str}, ... ],  # from # Schema rows (col-3)
    "joins": "<# Joins prose, stripped, or ''>",
    "examples": "<first ```...``` block body under # Examples, or ''>",
} }
```

A table is "enriched" iff any of: a non-empty `# Overview`, any non-empty column description, a non-empty `# Joins`, or a non-empty `# Examples`.

- [ ] **Step 1: Write the failing tests (append to test_okf.py)**

```python
class TestOKFGrounding:
    def _bundle(self, root, pay_runs_body):
        from apx_agent._okf import write_okf_bundle
        m = {"catalog": "c", "schema": "s", "tables": {
            "employees": ["employee_id(string)"],
            "pay_runs": ["gross_pay(decimal(6,2))", "employee_id(string)"],
        }}
        okf = root / "okf"
        write_okf_bundle(m, okf, timestamp="z")
        # overwrite pay_runs.md with an enriched body
        (okf / "tables" / "pay_runs.md").write_text(pay_runs_body)
        return okf

    def test_returns_none_when_no_enrichment(self, tmp_path):
        from apx_agent._okf import write_okf_bundle, okf_grounding
        m = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        okf = tmp_path / "okf"
        write_okf_bundle(m, okf, timestamp="z")  # bare auto-gen, blank descriptions
        assert okf_grounding(okf) is None

    def test_harvests_overview_joins_examples_and_col_descriptions(self, tmp_path):
        from apx_agent._okf import okf_grounding
        body = (
            "---\ntype: Unity Catalog Table\ntitle: pay_runs\n"
            "description: d\ntimestamp: z\n---\n\n"
            "# Overview\nOne row per employee per pay period.\n\n"
            "# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `gross_pay` | decimal(6,2) | Gross before deductions. |\n"
            "| `employee_id` | string | FK -> [`employees`](/tables/employees.md) |\n\n"
            "# Joins\nJoin to employees on `employee_id`.\n\n"
            "# Examples\n```sql\nSELECT * FROM pay_runs LIMIT 10\n```\n"
        )
        okf = self._bundle(tmp_path, body)
        g = okf_grounding(okf)
        assert g is not None
        assert "employees" not in g  # un-enriched table omitted
        pr = g["pay_runs"]
        assert pr["description"] == "One row per employee per pay period."
        assert pr["joins"] == "Join to employees on `employee_id`."
        assert pr["examples"].strip() == "SELECT * FROM pay_runs LIMIT 10"
        descmap = {c["name"]: c["description"] for c in pr["columns"]}
        assert descmap["gross_pay"] == "Gross before deductions."

    def test_malformed_returns_none_never_raises(self, tmp_path):
        from apx_agent._okf import okf_grounding
        okf = tmp_path / "okf" / "tables"
        okf.mkdir(parents=True)
        (okf.parent / "datasets").mkdir()
        (okf.parent / "datasets" / "s.md").write_text("---\n: bad: yaml\n---\n")
        assert okf_grounding(okf.parent) is None
```

- [ ] **Step 2: Run, expect FAIL** — `cd python && uv run pytest tests/test_okf.py::TestOKFGrounding -q` (ImportError: okf_grounding).

- [ ] **Step 3: Append to `_okf.py`** (reuses `_extract_section`, `_BACKTICK_IDENT`, `_ordered_table_files`, `OKFDocument`):

```python
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
        if not name_m:  # header / separator
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
    return m.group(1).strip() if m else section.strip()


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
```

- [ ] **Step 4: Run, expect PASS** — `cd python && uv run pytest tests/test_okf.py -q` (all prior + 4 new).

- [ ] **Step 5: Commit**
```bash
git add python/src/apx_agent/_okf.py python/tests/test_okf.py
git commit -m "feat(okf): okf_grounding harvests enrichment from bundle bodies"
```

---

## Task 2: `load_okf_grounding()` accessor in `_schema.py`

**Files:** Modify `python/src/apx_agent/_schema.py`; Test `python/tests/test_schema.py`.

Mirror `load_baked_schema`'s upward walk, returning the harvested enrichment for the first `.apx/okf/` found, else `None`. Totalised.

- [ ] **Step 1: Append tests to `test_schema.py`** (reuses the `TestLoadBakedSchemaOKF._write_okf` helper pattern; write a small local helper):

```python
class TestLoadOKFGrounding:
    def _write_enriched_okf(self, root):
        from apx_agent._okf import write_okf_bundle
        import shutil
        m = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(6,2))"]}}
        tmp = root / "okf_tmp"
        write_okf_bundle(m, tmp, timestamp="z")
        (tmp / "tables" / "pay_runs.md").write_text(
            "---\ntype: Unity Catalog Table\ntitle: pay_runs\ndescription: d\ntimestamp: z\n---\n\n"
            "# Overview\nPay records.\n\n# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `gross_pay` | decimal(6,2) |  |\n"
        )
        dest = root / ".apx" / "okf"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(dest))

    def test_returns_enrichment_when_present(self, tmp_path):
        from apx_agent._schema import load_okf_grounding
        self._write_enriched_okf(tmp_path)
        g = load_okf_grounding(tmp_path)
        assert g is not None and g["pay_runs"]["description"] == "Pay records."

    def test_returns_none_when_no_okf(self, tmp_path):
        from apx_agent._schema import load_okf_grounding
        assert load_okf_grounding(tmp_path) is None
```

- [ ] **Step 2: Run, expect FAIL** — `cd python && uv run pytest tests/test_schema.py::TestLoadOKFGrounding -q` (ImportError).

- [ ] **Step 3: Append to `_schema.py`** (after `load_baked_schema`):

```python
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
```

- [ ] **Step 4: Run, expect PASS** — `cd python && uv run pytest tests/test_schema.py -q`.

- [ ] **Step 5: Commit**
```bash
git add python/src/apx_agent/_schema.py python/tests/test_schema.py
git commit -m "feat(schema): load_okf_grounding accessor (upward walk, totalised)"
```

---

## Task 3: `grounding=` param + `_format_grounded_schema_block`

**Files:** Modify `python/src/apx_agent/_schema.py`; Test `python/tests/test_schema.py`.

Add `grounding: dict | None = None` to `build_instructions_from_schema`. In the tables-known branch, pick the schema block: `_format_grounded_schema_block(tables, grounding)` when `grounding` is truthy, else the existing `_format_schema_block(tables)`. **Default-identical** is the gate.

- [ ] **Step 1: Append tests to `test_schema.py`**

```python
class TestGroundedInstructions:
    def test_default_identical_when_grounding_none(self):
        from apx_agent._schema import build_instructions_from_schema
        tables = {"pay_runs": ["gross_pay(decimal(6,2))"], "employees": ["employee_id(string)"]}
        a = build_instructions_from_schema("c", "s", tables)
        b = build_instructions_from_schema("c", "s", tables, grounding=None)
        c = build_instructions_from_schema("c", "s", tables, grounding={})  # empty == none
        assert a == b == c

    def test_unenriched_table_line_unchanged_when_other_enriched(self):
        from apx_agent._schema import build_instructions_from_schema
        tables = {"pay_runs": ["gross_pay(decimal(6,2))"], "employees": ["employee_id(string)"]}
        grounding = {"pay_runs": {"description": "Pay records.", "columns": [], "joins": "", "examples": ""}}
        out = build_instructions_from_schema("c", "s", tables, grounding=grounding)
        # the enriched table gains a line; the un-enriched one is byte-identical to plain
        assert "- pay_runs: gross_pay(decimal(6,2))" in out
        assert "    Pay records." in out
        assert "- employees: employee_id(string)" in out

    def test_enrichment_includes_joins_and_example(self):
        from apx_agent._schema import build_instructions_from_schema
        tables = {"pay_runs": ["gross_pay(decimal(6,2))"]}
        grounding = {"pay_runs": {
            "description": "", "columns": [{"name": "gross_pay", "type": "decimal(6,2)", "description": "Gross."}],
            "joins": "Join employees on employee_id.", "examples": "SELECT * FROM pay_runs",
        }}
        out = build_instructions_from_schema("c", "s", tables, grounding=grounding)
        assert "    - gross_pay: Gross." in out
        assert "    Joins: Join employees on employee_id." in out
        assert "SELECT * FROM pay_runs" in out
```

- [ ] **Step 2: Run, expect FAIL** — `cd python && uv run pytest tests/test_schema.py::TestGroundedInstructions -q`.

- [ ] **Step 3: Implement.** Add `_format_grounded_schema_block` near `_format_schema_block`:

```python
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
            lines.append(f"    {enr['description']}")
        described = [c for c in enr.get("columns", []) if c.get("description")][:max_cols]
        for c in described:
            lines.append(f"    - {c['name']}: {c['description']}")
        if enr.get("joins"):
            lines.append(f"    Joins: {enr['joins']}")
        if enr.get("examples"):
            ex_lines = enr["examples"].strip().splitlines()[:6]
            lines.append("    Example:")
            lines.extend(f"      {l}" for l in ex_lines)
    if len(tables) > max_tables:
        lines.append(f"- (+{len(tables) - max_tables} more tables)")
    return "\n".join(lines)
```

Then change `build_instructions_from_schema`'s signature and the one block call. The signature becomes:
```python
def build_instructions_from_schema(
    catalog: str,
    schema: str,
    tables: dict[str, list[str]],
    persona: str | None = None,
    objective: str | None = None,
    grounding: dict | None = None,
) -> str:
```
In the tables-known branch, immediately before the `return (...)` that contains `f"Schema:\n{_format_schema_block(tables)}\n\n"`, compute the block and substitute:
```python
    _block = (
        _format_grounded_schema_block(tables, grounding)
        if grounding
        else _format_schema_block(tables)
    )
```
and replace `_format_schema_block(tables)` inside that f-string with `_block`. Leave the ungrounded (empty-`tables`) branch and all other text exactly as-is.

- [ ] **Step 4: Run, expect PASS** — `cd python && uv run pytest tests/test_schema.py -q` (incl. all pre-existing — default-identical must hold).

- [ ] **Step 5: Commit**
```bash
git add python/src/apx_agent/_schema.py python/tests/test_schema.py
git commit -m "feat(schema): grounding= param weaves OKF enrichment into instructions (default-identical)"
```

---

## Task 4: Wire `grounding=` into `data_agent.py` (baked-source only)

**Files:** Modify `python/src/apx_agent/data_agent.py`; Test `python/tests/test_data_agent.py`.

Pass `grounding=load_okf_grounding()` to `build_instructions_from_schema` **only when the resolved tables came from the baked path** — never from a `tables=` override or live introspection. Track this with a flag.

- [ ] **Step 1: Append a test to `test_data_agent.py`** (read the file's existing helpers/imports first to match style):

```python
class TestDataAgentOKFGrounding:
    def test_baked_grounding_reaches_instructions(self, tmp_path, monkeypatch):
        from apx_agent._okf import write_okf_bundle
        from apx_agent.data_agent import _build_data_tools_and_instructions

        m = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(6,2))"]}}
        okf = tmp_path / ".apx" / "okf"
        write_okf_bundle(m, okf, timestamp="z")
        (okf / "tables" / "pay_runs.md").write_text(
            "---\ntype: Unity Catalog Table\ntitle: pay_runs\ndescription: d\ntimestamp: z\n---\n\n"
            "# Overview\nPay records narrative.\n\n# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `gross_pay` | decimal(6,2) |  |\n"
        )
        monkeypatch.chdir(tmp_path)
        comp = _build_data_tools_and_instructions(
            catalog="c", schema="s", warehouse_id=None, ws=None, include_functions=False,
            genie_space=None, vector_index=None, instructions=None, persona=None,
            objective=None, tables=None, extra_tools=None,
        )
        assert "Pay records narrative." in comp.instructions  # enrichment reached the prompt

    def test_tables_override_does_not_pull_grounding(self, tmp_path, monkeypatch):
        # An explicit tables= override must NOT consult the OKF bundle for enrichment.
        from apx_agent._okf import write_okf_bundle
        from apx_agent.data_agent import _build_data_tools_and_instructions

        m = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(6,2))"]}}
        okf = tmp_path / ".apx" / "okf"
        write_okf_bundle(m, okf, timestamp="z")
        (okf / "tables" / "pay_runs.md").write_text(
            "---\ntype: Unity Catalog Table\ntitle: pay_runs\ndescription: d\ntimestamp: z\n---\n\n"
            "# Overview\nShould not appear.\n\n# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `gross_pay` | decimal(6,2) |  |\n"
        )
        monkeypatch.chdir(tmp_path)
        comp = _build_data_tools_and_instructions(
            catalog="c", schema="s", warehouse_id=None, ws=None, include_functions=False,
            genie_space=None, vector_index=None, instructions=None, persona=None,
            objective=None, tables={"pay_runs": ["gross_pay(decimal(6,2))"]}, extra_tools=None,
        )
        assert "Should not appear." not in comp.instructions
```

- [ ] **Step 2: Run, expect FAIL** — `cd python && uv run pytest tests/test_data_agent.py::TestDataAgentOKFGrounding -q` (the first asserts enrichment is present; before the change it won't be).

- [ ] **Step 3: Edit `_build_data_tools_and_instructions`.** (a) Add `from ._schema import build_instructions_from_schema, introspect_schema, load_baked_schema, load_okf_grounding` — extend the existing import at the top of `data_agent.py` (currently `from ._schema import build_instructions_from_schema, introspect_schema, load_baked_schema`) to include `load_okf_grounding`. (b) Track the baked source and pass grounding. Change the resolution block:

```python
    resolved_tables: dict = tables or {}
    baked_was_source = False
    if not resolved_tables and ws:
        resolved_tables = introspect_schema(ws, catalog, schema, warehouse_id)
    if not resolved_tables:
        baked = load_baked_schema()
        if (
            baked
            and baked.get("catalog") == catalog
            and baked.get("schema") == schema
            and isinstance(baked.get("tables"), dict)
        ):
            resolved_tables = baked["tables"]
            baked_was_source = True
```

and the instructions line at the end:

```python
    grounding = load_okf_grounding() if baked_was_source else None
    resolved_instructions = instructions or build_instructions_from_schema(
        catalog, schema, tables, persona=persona, objective=objective, grounding=grounding
    )
```

(Leave everything else — the warehouse check, resource attachment, tool assembly — untouched. Note `instructions or ...` still short-circuits: a user `instructions=` wins, full stop, per spec Q3.)

- [ ] **Step 4: Run, expect PASS** — `cd python && uv run pytest tests/test_data_agent.py -q` (new tests + all pre-existing).

- [ ] **Step 5: Commit**
```bash
git add python/src/apx_agent/data_agent.py python/tests/test_data_agent.py
git commit -m "feat(data-agent): wire OKF enrichment into the prompt (baked-source only)"
```

---

## Task 5: Spike — enrich payroll-coworker, prove it reaches the prompt

**Files:** Test `python/tests/test_okf_phase2_equivalence.py`; enrich (and commit) `python/payroll-coworker/.apx/okf/tables/pay_runs.md`.

Must EXECUTE and be INSPECTED. Two properties: (a) an un-enriched bundle yields a byte-identical prompt to Phase 1; (b) after enriching `pay_runs.md`, the enrichment appears in the built instructions while every other table's line is unchanged, and the token growth is bounded.

- [ ] **Step 1: Write `python/tests/test_okf_phase2_equivalence.py`**

```python
"""Phase-2 proof on the real payroll-coworker bundle: enrichment reaches the
prompt additively; an un-enriched bundle stays byte-identical to Phase 1."""
from __future__ import annotations

import json
from pathlib import Path

from apx_agent._okf import write_okf_bundle, okf_manifest, okf_grounding
from apx_agent._schema import build_instructions_from_schema

REAL_MANIFEST = Path(__file__).resolve().parents[1] / "payroll-coworker" / ".apx" / "schema.json"


def test_unenriched_bundle_prompt_identical(tmp_path):
    m = json.loads(REAL_MANIFEST.read_text())
    write_okf_bundle(m, tmp_path / "okf", timestamp="z")  # bare auto-gen, no enrichment
    tables = okf_manifest(tmp_path / "okf")["tables"]
    grounding = okf_grounding(tmp_path / "okf")
    assert grounding is None  # nothing enriched
    plain = build_instructions_from_schema(m["catalog"], m["schema"], tables)
    grounded = build_instructions_from_schema(m["catalog"], m["schema"], tables, grounding=grounding)
    assert grounded == plain  # default-identical on the real schema


def test_committed_enriched_bundle_surfaces_in_prompt():
    # The committed payroll bundle has an enriched pay_runs.md (Step 3 below).
    okf_root = REAL_MANIFEST.parent / "okf"
    m = okf_manifest(okf_root)
    grounding = okf_grounding(okf_root)
    assert grounding is not None and "pay_runs" in grounding
    out = build_instructions_from_schema(m["catalog"], m["schema"], m["tables"], grounding=grounding)
    assert "# Joins" not in out  # we emit distilled prose, not raw headings
    # the enriched join prose reaches the prompt
    assert "employee_id" in out
    # an un-enriched table's line is still present and plain
    assert "- employees:" in out
```

- [ ] **Step 2: Run** — `cd python && uv run pytest tests/test_okf_phase2_equivalence.py::test_unenriched_bundle_prompt_identical -q` → expect PASS. (The second test fails until Step 3 enriches the committed bundle.)

- [ ] **Step 3: Enrich the committed `pay_runs.md` (EXECUTE the edit).** Open `python/payroll-coworker/.apx/okf/tables/pay_runs.md` and add enrichment bodies after the existing `# Schema` table — a `# Overview`, fill a couple of Description cells, a `# Joins` referencing `employee_id`, and a `# Examples` sql block. Keep the existing frontmatter and `# Schema` rows intact (so `okf_manifest` still round-trips — the derived `schema.json` cache must remain valid). Example addition:

```markdown
# Overview
One row per employee per pay period; the core payroll fact table.

# Joins
Join to [`employees`](/tables/employees.md) on `employee_id` to attribute pay to a worker.

# Examples
```sql
SELECT employee_id, gross_pay FROM pay_runs WHERE period_end = '2026-05-31'
```
```

- [ ] **Step 4: Verify the cache still matches (the Phase-1 invariant must survive enrichment).**
```bash
cd /Users/stuart.gano/Documents/apx-agent-okf-phase2/python
uv run python - <<'PY'
from apx_agent._okf import okf_manifest
import json, pathlib
root = pathlib.Path("payroll-coworker/.apx/okf")
cache = json.loads(pathlib.Path("payroll-coworker/.apx/schema.json").read_text())
assert okf_manifest(root) == cache, "enrichment broke the schema round-trip"
print("OK: enriched bundle still round-trips to the committed cache")
PY
```
Expected: `OK: ...`. If it fails, you changed a `# Schema` row or the frontmatter — revert that part.

- [ ] **Step 5: Run the Phase-2 equivalence file + the full suite.**
```bash
cd /Users/stuart.gano/Documents/apx-agent-okf-phase2/python
uv run pytest tests/test_okf_phase2_equivalence.py -q
uv run pytest -q 2>&1 | grep -E "passed|failed|error" | tail -2
```
Expected: both green; full suite shows N passed, 0 failed.

- [ ] **Step 6: Inspect the built prompt by eye (EXECUTE + INSPECT).**
```bash
cd /Users/stuart.gano/Documents/apx-agent-okf-phase2/python
uv run python - <<'PY'
from apx_agent._okf import okf_manifest, okf_grounding
from apx_agent._schema import build_instructions_from_schema
root = "payroll-coworker/.apx/okf"
m = okf_manifest(root); g = okf_grounding(root)
plain = build_instructions_from_schema(m["catalog"], m["schema"], m["tables"])
grounded = build_instructions_from_schema(m["catalog"], m["schema"], m["tables"], grounding=g)
print("=== GROUNDED PROMPT ===\n", grounded)
print("\n=== delta chars:", len(grounded) - len(plain))
PY
```
Confirm the `pay_runs` enrichment appears indented under its line, other tables unchanged, and the char delta is modest (bounded).

- [ ] **Step 7: Commit**
```bash
cd /Users/stuart.gano/Documents/apx-agent-okf-phase2
git add python/tests/test_okf_phase2_equivalence.py python/payroll-coworker/.apx/okf/tables/pay_runs.md
git commit -m "test(okf): phase-2 enrichment reaches the prompt on payroll-coworker"
```

---

## Self-review notes

- **Spec §6 coverage:** `load_okf_grounding()` → Task 2; `grounding=` param + `_format_grounded_schema_block` → Task 3; the single `data_agent.py` pass-through (baked-source only) → Task 4; F7 (keep all tables) → Task 3 `_format_grounded_schema_block` + its `test_unenriched_table_line_unchanged`; token bounding (caps mirror `_format_schema_block`) → Task 3 + Task 5 Step 6; spec Q3 (`instructions=` wins) → preserved by the `instructions or ...` short-circuit, Task 4.
- **Default-identical gate** appears in Task 3 (`test_default_identical_when_grounding_none`) and Task 5 (`test_unenriched_bundle_prompt_identical` on the real schema) — the load-bearing invariant.
- **Totality:** `okf_grounding` (Task 1) and `load_okf_grounding` (Task 2) both None-on-error, never raise — they run in the deployed container.
- **Name consistency:** `okf_grounding` (`_okf.py`), `load_okf_grounding` (`_schema.py`), `_format_grounded_schema_block`, `grounding=` param, `baked_was_source` flag — used identically across tasks.
