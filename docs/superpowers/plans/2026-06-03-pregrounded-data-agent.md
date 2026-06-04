# Pre-grounded DataAgent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bake a UC schema manifest at `apx scaffold` so the data-agent example arrives pre-grounded — answers directly (no `SHOW TABLES`), lists real tables/columns in its instructions, and shows a schema card on the chat landing.

**Architecture:** `apx scaffold` introspects the schema via the Tables API (no warehouse needed) and writes `.apx/schema.json`. At serve time `DataAgent` auto-discovers the manifest (no `ws` at boot), `build_instructions_from_schema` lists the columns and drops the discovery line, and the landing renders a data card from the manifest carried on `AgentContext`. `apx refresh-schema` rewrites the manifest on drift.

**Tech Stack:** Python, Databricks SDK (`WorkspaceClient.tables.list`), Click CLI, pytest. No new deps.

**Spec:** `docs/superpowers/specs/2026-06-03-pregrounded-data-agent-design.md`

---

## File structure

- `python/src/apx_agent/_schema.py` — manifest constants, `load_baked_schema()` loader, `introspect_schema_columns()` (Tables-API, no warehouse), `_format_schema_block()` helper, and the grounded-instruction rewrite in `build_instructions_from_schema()`.
- `python/src/apx_agent/data_agent.py` — new `tables=` param + resolution order (explicit → live introspect via `ws` → baked manifest → `{}`).
- `python/src/apx_agent/cli.py` — `_schema_manifest_for_scaffold()`, write `.apx/schema.json` in both scaffolds, copy `.apx` into the deploy bundle, `apx refresh-schema` command.
- `python/src/apx_agent/_models.py` — `AgentContext` carries an optional `schema` manifest.
- `python/src/apx_agent/_wiring.py` — load the manifest once and attach it to `AgentContext`.
- `python/src/apx_agent/_ui_chat.py` — landing data card from `ctx.schema`.
- Tests: `tests/test_schema.py` (new), `tests/test_data_agent.py`, `tests/test_cli.py`, `tests/test_dev_ui_routes.py`.

**Naming note:** `DataAgent`'s existing positional `schema` is the schema *name* (a `str`). The new baked-tables parameter is therefore named **`tables`** (a `dict[str, list[str]]`), not `schema`, to avoid the collision. The spec's `schema: dict` refers to this same concept.

---

### Task 1: Manifest constants + `load_baked_schema` loader

**Files:**
- Modify: `python/src/apx_agent/_schema.py`
- Test: `python/tests/test_schema.py` (create)

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_schema.py`:
```python
"""Tests for _schema.py — manifest loading, Tables-API introspection, grounding."""
from __future__ import annotations

import json
from pathlib import Path

from apx_agent._schema import load_baked_schema, APX_DIR, SCHEMA_MANIFEST_NAME


def _write_manifest(root: Path, manifest: dict) -> None:
    d = root / APX_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / SCHEMA_MANIFEST_NAME).write_text(json.dumps(manifest))


class TestLoadBakedSchema:
    def test_loads_from_start_dir(self, tmp_path):
        m = {"catalog": "samples", "schema": "tpch", "tables": {"customer": ["c_custkey(bigint)"]}}
        _write_manifest(tmp_path, m)
        assert load_baked_schema(tmp_path) == m

    def test_walks_up_from_nested_dir(self, tmp_path):
        m = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        _write_manifest(tmp_path, m)
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert load_baked_schema(nested) == m

    def test_missing_returns_none(self, tmp_path):
        assert load_baked_schema(tmp_path) is None

    def test_corrupt_json_returns_none(self, tmp_path):
        d = tmp_path / APX_DIR
        d.mkdir()
        (d / SCHEMA_MANIFEST_NAME).write_text("{not valid json")
        assert load_baked_schema(tmp_path) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_schema.py::TestLoadBakedSchema -q`
Expected: FAIL — `ImportError: cannot import name 'load_baked_schema'`

- [ ] **Step 3: Implement the loader**

In `python/src/apx_agent/_schema.py`, add after the module docstring imports (top of file, after `from typing import Any`):
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_schema.py::TestLoadBakedSchema -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/_schema.py tests/test_schema.py
git commit -m "feat(schema): load_baked_schema manifest loader"
```

---

### Task 2: Tables-API schema introspection (no warehouse)

**Files:**
- Modify: `python/src/apx_agent/_schema.py`
- Test: `python/tests/test_schema.py`

Rationale: `introspect_schema()` runs a SQL statement and needs a warehouse, which the scaffold doesn't have. The Tables API (`ws.tables.list`) returns columns without a warehouse — the right path at scaffold time. `col.type_text` is the type string (see `catalog.py` `_describe_table`).

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_schema.py`:
```python
class TestIntrospectViaTablesApi:
    def test_builds_table_to_columns_map(self):
        from types import SimpleNamespace
        from apx_agent._schema import introspect_schema_columns

        def col(name, type_text):
            return SimpleNamespace(name=name, type_text=type_text)

        tables = [
            SimpleNamespace(name="customer", columns=[col("c_custkey", "bigint"), col("c_name", "string")]),
            SimpleNamespace(name="orders", columns=[col("o_orderkey", "bigint")]),
        ]
        ws = SimpleNamespace(tables=SimpleNamespace(list=lambda catalog_name, schema_name: tables))
        out = introspect_schema_columns(ws, "samples", "tpch")
        assert out == {
            "customer": ["c_custkey(bigint)", "c_name(string)"],
            "orders": ["o_orderkey(bigint)"],
        }

    def test_returns_empty_on_failure(self):
        from types import SimpleNamespace
        from apx_agent._schema import introspect_schema_columns

        def boom(**_):
            raise RuntimeError("no perms")
        ws = SimpleNamespace(tables=SimpleNamespace(list=boom))
        assert introspect_schema_columns(ws, "c", "s") == {}

    def test_none_ws_returns_empty(self):
        from apx_agent._schema import introspect_schema_columns
        assert introspect_schema_columns(None, "c", "s") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_schema.py::TestIntrospectViaTablesApi -q`
Expected: FAIL — `ImportError: cannot import name 'introspect_schema_columns'`

- [ ] **Step 3: Implement**

In `python/src/apx_agent/_schema.py`, add after `introspect_schema`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_schema.py::TestIntrospectViaTablesApi -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/_schema.py tests/test_schema.py
git commit -m "feat(schema): Tables-API column introspection (no warehouse)"
```

---

### Task 3: Grounded instructions list columns + drop the discovery line

**Files:**
- Modify: `python/src/apx_agent/_schema.py:58-102` (`build_instructions_from_schema`)
- Test: `python/tests/test_schema.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_schema.py`:
```python
class TestBuildInstructions:
    DISCOVERY = "call the SQL tool to confirm what tables and columns are available"

    def test_grounded_lists_columns_and_drops_discovery(self):
        from apx_agent._schema import build_instructions_from_schema
        tables = {
            "customer": ["c_custkey(bigint)", "c_name(string)", "c_acctbal(decimal)"],
            "orders": ["o_orderkey(bigint)", "o_custkey(bigint)"],
        }
        out = build_instructions_from_schema("samples", "tpch", tables)
        # Lists real tables + columns
        assert "customer" in out and "c_custkey(bigint)" in out
        assert "orders" in out and "o_orderkey(bigint)" in out
        # Drops the "go discover the schema" instruction
        assert self.DISCOVERY not in out
        # Tells it not to rediscover
        assert "SHOW TABLES" in out

    def test_ungrounded_keeps_discovery(self):
        from apx_agent._schema import build_instructions_from_schema
        out = build_instructions_from_schema("samples", "tpch", {})
        assert self.DISCOVERY in out  # no tables known → still tells it to discover

    def test_column_cap(self):
        from apx_agent._schema import build_instructions_from_schema
        cols = [f"col{i}(int)" for i in range(40)]
        out = build_instructions_from_schema("c", "s", {"big": cols})
        assert "col0(int)" in out
        assert "col39(int)" not in out      # capped
        assert "more" in out.lower()        # "+N more" hint
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_schema.py::TestBuildInstructions -q`
Expected: FAIL — grounded output currently contains the discovery line and no column listing.

- [ ] **Step 3: Implement**

Replace the entire body of `build_instructions_from_schema` in `python/src/apx_agent/_schema.py` (lines 58–102) with:
```python
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


def build_instructions_from_schema(
    catalog: str,
    schema: str,
    tables: dict[str, list[str]],
) -> str:
    """Build agent instructions from schema metadata without an LLM call.

    When tables (with columns) are known, the instructions LIST the schema and
    tell the agent to query directly — no discovery step. When no tables are
    known, the agent is told to discover the schema with the SQL tool first.
    """
    fqn = f"{catalog}.{schema}" if catalog and schema else schema or catalog or "the data"
    table_names = list(tables.keys())

    if not table_names:
        # Ungrounded: nothing known — tell the agent to discover first.
        return (
            f"You are a data assistant for {fqn}. Your data includes: {fqn}.\n\n"
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

    return (
        f"You are a data assistant for {fqn}. You already know the schema below — "
        f"query the relevant table directly with the SQL tool. Do NOT run "
        f"SHOW TABLES or DESCRIBE to discover the structure; it is given here.\n\n"
        f"Schema:\n{_format_schema_block(tables)}\n\n"
        f"{chain}\n\n"
        f"When a query returns empty results or an error, try a broader filter or "
        f"verify the column name exists in the schema before telling the user you "
        f"cannot help.\n\n"
        f"Always base your answers on tool results. Never estimate or fabricate "
        f"data values. If you cannot retrieve what was asked, say so clearly and "
        f"describe what you can provide."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_schema.py -q`
Expected: PASS (all classes). Then check no caller regressed: `cd python && uv run pytest tests/test_data_agent.py -q`
Expected: PASS (existing tests that assert ungrounded behavior still pass — the `{}` branch text is unchanged for the discovery line).

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/_schema.py tests/test_schema.py
git commit -m "feat(schema): grounded instructions list columns + drop discovery line"
```

---

### Task 4: DataAgent accepts baked tables + auto-discovers the manifest

**Files:**
- Modify: `python/src/apx_agent/data_agent.py:39-83` (`_build_data_tools_and_instructions`) and `:109-143` (`DataAgent.__init__`)
- Test: `python/tests/test_data_agent.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_data_agent.py`:
```python
class TestDataAgentBakedSchema:
    def test_explicit_tables_ground_instructions(self):
        from apx_agent import DataAgent
        agent = DataAgent(
            "samples", "tpch",
            tables={"customer": ["c_custkey(bigint)", "c_name(string)"]},
        )
        instr = agent._instructions
        assert "customer" in instr and "c_custkey(bigint)" in instr
        assert "call the SQL tool to confirm what tables" not in instr

    def test_auto_discovers_manifest(self, tmp_path, monkeypatch):
        import json
        from apx_agent._schema import APX_DIR, SCHEMA_MANIFEST_NAME
        from apx_agent import DataAgent
        d = tmp_path / APX_DIR
        d.mkdir()
        (d / SCHEMA_MANIFEST_NAME).write_text(json.dumps({
            "catalog": "samples", "schema": "tpch",
            "tables": {"orders": ["o_orderkey(bigint)"]},
        }))
        monkeypatch.chdir(tmp_path)
        agent = DataAgent("samples", "tpch")
        assert "orders" in agent._instructions and "o_orderkey(bigint)" in agent._instructions
        assert "call the SQL tool to confirm what tables" not in agent._instructions

    def test_manifest_for_other_schema_ignored(self, tmp_path, monkeypatch):
        import json
        from apx_agent._schema import APX_DIR, SCHEMA_MANIFEST_NAME
        from apx_agent import DataAgent
        d = tmp_path / APX_DIR
        d.mkdir()
        (d / SCHEMA_MANIFEST_NAME).write_text(json.dumps({
            "catalog": "other", "schema": "elsewhere",
            "tables": {"x": ["a(int)"]},
        }))
        monkeypatch.chdir(tmp_path)
        agent = DataAgent("samples", "tpch")  # different schema → ignore manifest
        assert "call the SQL tool to confirm what tables" in agent._instructions

    def test_no_manifest_falls_back(self, tmp_path, monkeypatch):
        from apx_agent import DataAgent
        monkeypatch.chdir(tmp_path)
        agent = DataAgent("samples", "tpch")
        assert "call the SQL tool to confirm what tables" in agent._instructions
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_data_agent.py::TestDataAgentBakedSchema -q`
Expected: FAIL — `DataAgent` has no `tables` kwarg / does not auto-discover.

- [ ] **Step 3: Implement**

In `python/src/apx_agent/data_agent.py`, update the import line (currently `from ._schema import build_instructions_from_schema, introspect_schema`) to:
```python
from ._schema import build_instructions_from_schema, introspect_schema, load_baked_schema
```

In `_build_data_tools_and_instructions`, add a `tables` parameter and replace the introspection line. Change the signature line `instructions: str | None,` block to include `tables: dict | None,` (add before `extra_tools`), and replace:
```python
    # Introspect once at construction (best-effort) when a client is given.
    tables = introspect_schema(ws, catalog, schema, warehouse_id) if ws else {}
```
with:
```python
    # Resolve the schema (table -> columns), in priority order:
    #   1) explicit `tables=` override
    #   2) live introspection when a workspace client is given
    #   3) the baked `.apx/schema.json` manifest (scaffold-time grounding)
    #   4) {} -> generic, ungrounded instructions
    resolved_tables: dict = tables or {}
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
    tables = resolved_tables
```
(The remaining body already uses the local name `tables` for `attach_resources` and `build_instructions_from_schema`, so no further change there.)

In `DataAgent.__init__`, add the parameter (after `instructions: str | None = None,`):
```python
        tables: dict | None = None,
```
and pass it through to the builder call (add `tables=tables,` alongside the other kwargs in `_build_data_tools_and_instructions(...)`).

Document it in the `DataAgent` docstring Args (after the `instructions:` entry):
```
        tables: Pre-baked schema as ``{table: ["col(type)", ...]}`` (e.g. the
            ``.apx/schema.json`` manifest). Grounds the agent without a live
            workspace call. When omitted, falls back to live introspection
            (if ``ws`` given) then auto-discovery of ``.apx/schema.json``.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_data_agent.py -q`
Expected: PASS (new class + existing tests).

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/data_agent.py tests/test_data_agent.py
git commit -m "feat(data-agent): accept baked tables + auto-discover .apx/schema.json"
```

---

### Task 5: Scaffold writes `.apx/schema.json` + deploy bundle includes it

**Files:**
- Modify: `python/src/apx_agent/cli.py` — add `_schema_manifest_for_scaffold()`, write manifest in `_scaffold_apps` (`:1098-1166`) and `_scaffold_model_serving` (`:1012-...`), add `cp -r .apx` to `_SCAFFOLD_APPS_DATABRICKS_YML` build script (`:789-800`).
- Test: `python/tests/test_cli.py`

Note: the scaffold `.gitignore` (`_SCAFFOLD_GITIGNORE`) ignores `.apx-builder.json` (a file), NOT the `.apx/` directory, so `.apx/schema.json` is committable already — no `.gitignore` change needed.

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_cli.py`:
```python
class TestScaffoldSchemaManifest:
    def test_manifest_built_from_introspection(self, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(
            cli, "introspect_schema_columns",
            lambda ws, c, s: {"customer": ["c_custkey(bigint)"]},
        )
        # _make_ws_for_scaffold returns a dummy; introspection is what we stubbed
        monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda profile: object())
        m = cli._schema_manifest_for_scaffold("samples", "tpch", profile=None)
        assert m == {
            "catalog": "samples", "schema": "tpch",
            "tables": {"customer": ["c_custkey(bigint)"]},
        }

    def test_manifest_none_when_empty(self, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(cli, "introspect_schema_columns", lambda ws, c, s: {})
        monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda profile: object())
        assert cli._schema_manifest_for_scaffold("c", "s", profile=None) is None

    def test_manifest_none_when_no_ws(self, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(cli, "_make_ws_for_scaffold", lambda profile: None)
        assert cli._schema_manifest_for_scaffold("c", "s", profile=None) is None

    def test_apps_scaffold_writes_manifest(self, tmp_path, monkeypatch):
        import json
        from apx_agent import cli
        monkeypatch.setattr(
            cli, "_schema_manifest_for_scaffold",
            lambda c, s, profile=None: {"catalog": c, "schema": s, "tables": {"t": ["a(int)"]}},
        )
        cli._scaffold_apps(tmp_path, "demo", force=True, catalog="samples", schema="tpch", table="t")
        manifest = tmp_path / ".apx" / "schema.json"
        assert manifest.is_file()
        assert json.loads(manifest.read_text())["tables"] == {"t": ["a(int)"]}

    def test_apps_scaffold_no_manifest_when_none(self, tmp_path, monkeypatch):
        from apx_agent import cli
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold", lambda c, s, profile=None: None)
        cli._scaffold_apps(tmp_path, "demo", force=True, catalog="samples", schema="tpch", table="t")
        assert not (tmp_path / ".apx" / "schema.json").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_cli.py::TestScaffoldSchemaManifest -q`
Expected: FAIL — `_schema_manifest_for_scaffold` / `_make_ws_for_scaffold` not defined.

- [ ] **Step 3: Implement**

In `python/src/apx_agent/cli.py`, add a **module-level** import alongside the other top-of-file imports (the test does `monkeypatch.setattr(cli, "introspect_schema_columns", ...)`, so it must be a module-level name, not a function-local import):
```python
from ._schema import introspect_schema_columns
```

Then add these two helpers next to `_discover_default_data` (after `_probe_first_table`, ~line 981):
```python
def _make_ws_for_scaffold(profile: str | None):
    """Build a WorkspaceClient for scaffold-time introspection, or None."""
    try:
        from databricks.sdk import WorkspaceClient
        return WorkspaceClient(profile=profile) if profile else WorkspaceClient()
    except Exception:
        return None


def _schema_manifest_for_scaffold(
    catalog: str, schema: str, profile: str | None = None
) -> "dict | None":
    """Introspect ``catalog.schema`` via the Tables API and return a manifest
    dict ``{catalog, schema, tables}``, or ``None`` when nothing is readable.

    Calls the module-level ``introspect_schema_columns`` (so tests can stub it).
    """
    ws = _make_ws_for_scaffold(profile)
    if ws is None:
        return None
    tables = introspect_schema_columns(ws, catalog, schema)
    if not tables:
        return None
    return {"catalog": catalog, "schema": schema, "tables": tables}
```

In `_scaffold_apps` (after `prelude, extra_tools = _example_tool_block(...)`, ~line 1107) add:
```python
    import json as _json
    manifest = _schema_manifest_for_scaffold(catalog, schema)
```
and in the `files` dict (after the `"agent.py": ...` entry), conditionally include the manifest by adding, just before the `for rel_path, content in files.items()` loop:
```python
    if manifest is not None:
        files[".apx/schema.json"] = _json.dumps(manifest, indent=2)
```

Do the same in `_scaffold_model_serving` (mirror: build `manifest`, add to `files` when not None).

In `_SCAFFOLD_APPS_DATABRICKS_YML`, in the `artifacts.default.build` script (the `cp -r agent_server ...` block, ~line 796), add a line:
```yaml
      cp -r .apx .build/ 2>/dev/null || true
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_cli.py::TestScaffoldSchemaManifest -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/cli.py tests/test_cli.py
git commit -m "feat(scaffold): bake .apx/schema.json + include it in the deploy bundle"
```

---

### Task 6: `apx refresh-schema` command

**Files:**
- Modify: `python/src/apx_agent/cli.py` (add a Click command near `scaffold`)
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_cli.py`:
```python
class TestRefreshSchema:
    def test_refresh_rewrites_manifest(self, tmp_path, monkeypatch):
        import json
        from click.testing import CliRunner
        from apx_agent import cli
        # existing manifest pins samples.tpch
        d = tmp_path / ".apx"; d.mkdir()
        (d / "schema.json").write_text(json.dumps(
            {"catalog": "samples", "schema": "tpch", "tables": {"old": ["a(int)"]}}))
        monkeypatch.setattr(
            cli, "_schema_manifest_for_scaffold",
            lambda c, s, profile=None: {"catalog": c, "schema": s, "tables": {"new": ["b(int)"]}},
        )
        monkeypatch.chdir(tmp_path)
        res = CliRunner().invoke(cli.main, ["refresh-schema"])
        assert res.exit_code == 0, res.output
        assert json.loads((d / "schema.json").read_text())["tables"] == {"new": ["b(int)"]}

    def test_refresh_errors_without_existing_manifest(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from apx_agent import cli
        monkeypatch.chdir(tmp_path)
        res = CliRunner().invoke(cli.main, ["refresh-schema"])
        assert res.exit_code != 0
        assert "no .apx/schema.json" in res.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_cli.py::TestRefreshSchema -q`
Expected: FAIL — no `refresh-schema` command.

- [ ] **Step 3: Implement**

In `python/src/apx_agent/cli.py`, add after the `scaffold` command:
```python
@main.command("refresh-schema")
@click.option("--profile", default=None,
              help="Databricks CLI profile to introspect with. "
                   "Falls back to $DATABRICKS_CONFIG_PROFILE.")
def refresh_schema(profile: str | None) -> None:
    """Re-introspect this project's catalog.schema and rewrite .apx/schema.json.

    Run inside a scaffolded project. Reads the existing manifest to learn which
    catalog/schema to refresh, re-introspects via the Tables API, and overwrites
    the manifest so the agent's grounding + landing card reflect the live schema.
    """
    import json as _json
    from ._schema import load_baked_schema, APX_DIR, SCHEMA_MANIFEST_NAME

    existing = load_baked_schema(Path.cwd())
    if not existing or not existing.get("catalog") or not existing.get("schema"):
        raise click.ClickException(
            "no .apx/schema.json found in this project — run `apx scaffold` first "
            "(or create the manifest) so I know which catalog.schema to refresh."
        )
    catalog, schema = existing["catalog"], existing["schema"]
    manifest = _schema_manifest_for_scaffold(catalog, schema, profile=profile)
    if manifest is None:
        raise click.ClickException(
            f"could not read tables for {catalog}.{schema} — check your profile "
            f"and Unity Catalog grants."
        )
    out = Path.cwd() / APX_DIR / SCHEMA_MANIFEST_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_json.dumps(manifest, indent=2))
    n = len(manifest["tables"])
    click.echo(f"refreshed {out} — {n} table{'s' if n != 1 else ''} from {catalog}.{schema}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_cli.py::TestRefreshSchema -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/cli.py tests/test_cli.py
git commit -m "feat(cli): apx refresh-schema rewrites the baked manifest"
```

---

### Task 7: AgentContext carries the manifest; serve-time load

**Files:**
- Modify: `python/src/apx_agent/_models.py:272-289` (`AgentContext`)
- Modify: `python/src/apx_agent/_wiring.py:430` (context construction)
- Test: `python/tests/test_dev_ui_routes.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_dev_ui_routes.py`:
```python
class TestAgentContextSchema:
    def test_context_accepts_schema(self):
        from apx_agent import AgentConfig, AgentContext
        cfg = AgentConfig(name="d", description="x", examples=[])
        ctx = AgentContext(
            config=cfg, tools=[], card={"name": "d", "skills": []},
            agent=None,  # type: ignore[arg-type]
            schema={"catalog": "samples", "schema": "tpch", "tables": {"t": ["a(int)"]}},
        )
        assert ctx.schema["tables"] == {"t": ["a(int)"]}

    def test_schema_defaults_none(self):
        from apx_agent import AgentConfig, AgentContext
        cfg = AgentConfig(name="d", description="x", examples=[])
        ctx = AgentContext(config=cfg, tools=[], card={"name": "d", "skills": []},
                           agent=None)  # type: ignore[arg-type]
        assert ctx.schema is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_dev_ui_routes.py::TestAgentContextSchema -q`
Expected: FAIL — `AgentContext.__init__` has no `schema` param.

- [ ] **Step 3: Implement**

In `python/src/apx_agent/_models.py`, update `AgentContext.__init__` to add the optional field:
```python
    def __init__(
        self,
        config: AgentConfig,
        tools: list[AgentTool],
        card: AgentCard,
        agent: "BaseAgent",
        schema: "dict | None" = None,
    ):
        self.config = config
        self.tools = tools
        self.card = card
        self.agent = agent
        self.schema = schema
        self._tool_map: dict[str, AgentTool] = {t.name: t for t in tools}
```

In `python/src/apx_agent/_wiring.py` at line 430, change:
```python
    ctx = AgentContext(config=config, tools=tools, card=card, agent=agent)
```
to:
```python
    from ._schema import load_baked_schema
    ctx = AgentContext(
        config=config, tools=tools, card=card, agent=agent,
        schema=load_baked_schema(),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_dev_ui_routes.py::TestAgentContextSchema -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/_models.py src/apx_agent/_wiring.py tests/test_dev_ui_routes.py
git commit -m "feat(context): carry baked schema manifest on AgentContext"
```

---

### Task 8: Landing data card

**Files:**
- Modify: `python/src/apx_agent/_ui_chat.py:414-454` (`_render_landing`) + the `<style>` block
- Test: `python/tests/test_dev_ui_routes.py`

- [ ] **Step 1: Write the failing test**

Append to `python/tests/test_dev_ui_routes.py`:
```python
class TestLandingDataCard:
    def _ctx(self, schema):
        from apx_agent import AgentConfig, AgentContext
        cfg = AgentConfig(name="d", description="x", examples=[])
        return AgentContext(config=cfg, tools=[], card={"name": "d", "skills": []},
                            agent=None, schema=schema)  # type: ignore[arg-type]

    def test_card_renders_tables_and_columns(self):
        from apx_agent._ui_chat import _render_landing
        ctx = self._ctx({"catalog": "samples", "schema": "tpch",
                         "tables": {"customer": ["c_custkey(bigint)", "c_name(string)"]}})
        html = _render_landing(ctx)
        assert "samples.tpch" in html
        assert "customer" in html
        assert "c_custkey" in html
        assert "data-card" in html  # the card container styling/class

    def test_card_omitted_without_schema(self):
        from apx_agent._ui_chat import _render_landing
        html = _render_landing(self._ctx(None))
        assert "data-card" not in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_dev_ui_routes.py::TestLandingDataCard -q`
Expected: FAIL — no data card rendered.

- [ ] **Step 3: Implement**

In `python/src/apx_agent/_ui_chat.py::_render_landing`, after the `if desc:` block and before `if tools:`, add:
```python
    schema = getattr(ctx, "schema", None)
    if schema and isinstance(schema.get("tables"), dict) and schema["tables"]:
        fqn = f'{schema.get("catalog", "")}.{schema.get("schema", "")}'.strip(".")
        tbls = schema["tables"]
        rows = ""
        for tname, cols in list(tbls.items())[:12]:
            col_names = [c.split("(")[0] for c in (cols or [])][:6]
            shown = ", ".join(col_names)
            if len(cols or []) > 6:
                shown += " …"
            rows += (f'<div class="data-row"><span class="data-tbl">{_html.escape(tname)}</span>'
                     f'<span class="data-cols">{_html.escape(shown)}</span></div>')
        more = f' <span class="data-more">(+{len(tbls) - 12} more)</span>' if len(tbls) > 12 else ""
        parts.append(
            '<div class="data-card">'
            f'<div class="data-card-head">I understand <code>{_html.escape(fqn)}</code> '
            f'— {len(tbls)} table{"s" if len(tbls) != 1 else ""}{more}</div>'
            f'{rows}</div>'
        )
```

In the `<style>` block of `_render_agent_ui` (search for an existing landing rule like `.landing-hi` and add nearby):
```css
  .data-card {{ background: #0e1116; border: 1px solid #1f242b; border-radius: 10px;
                padding: 12px 14px; margin: 10px 0; max-width: 680px; }}
  .data-card-head {{ font-size: 12.5px; color: #9aa3ad; margin-bottom: 8px; }}
  .data-card-head code {{ color: #60b0ff; }}
  .data-row {{ display: flex; gap: 10px; font-size: 12px; padding: 2px 0; }}
  .data-tbl {{ flex: none; min-width: 110px; color: #cfe; font-family: ui-monospace, monospace; }}
  .data-cols {{ color: #6b7280; font-family: ui-monospace, monospace; overflow: hidden;
                text-overflow: ellipsis; white-space: nowrap; }}
  .data-more {{ color: #6b7280; }}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_dev_ui_routes.py::TestLandingDataCard -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
git add src/apx_agent/_ui_chat.py tests/test_dev_ui_routes.py
git commit -m "feat(dev-ui): landing data card showing the grounded schema"
```

---

### Task 9: Gate — full suite + types

- [ ] **Step 1:** `cd python && git checkout -- uv.lock 2>/dev/null || true`
- [ ] **Step 2:** `cd python && uv run pytest -q` — all pass (baseline ~1858 + new ~25).
- [ ] **Step 3:** `cd python && uv run pyright src/apx_agent` — 0 errors (the pre-existing `_tool.py:138` warning is acceptable).
- [ ] **Step 4:** `cd python && git checkout -- uv.lock 2>/dev/null || true && git status --short` — only the intended files; `uv.lock` clean.

---

## Self-review

**Spec coverage:**
- Scaffold-time introspection → manifest → **Task 5** (Tables-API path: **Task 2**).
- DataAgent auto-loads manifest, no ws at boot → **Task 4** (loader: **Task 1**).
- Grounded instructions list columns + drop discovery line → **Task 3**.
- Landing data card → **Task 8** (context carries manifest: **Task 7**).
- Refresh command → **Task 6**.
- Packaging: `.apx/schema.json` committed (gitignore already permits) + copied into deploy bundle → **Task 5**.
- Single source of truth (manifest stores structured schema only; instructions/card rebuilt at runtime) → Tasks 3, 8 rebuild from the manifest.
- Degradation (introspection/manifest failures → generic fallback) → Tasks 1, 2, 4 (all best-effort, tested).

**Placeholder scan:** No `TBD`/`TODO`/"implement later". All code steps show complete code. (An earlier confusing fake-import line in Task 5 was removed in favor of a clean module-level import instruction.)

**Type/name consistency:** `load_baked_schema`, `introspect_schema_columns`, `_format_schema_block`, `build_instructions_from_schema` (signature unchanged), `_schema_manifest_for_scaffold`, `_make_ws_for_scaffold`, `refresh-schema`, `AgentContext(schema=)`, `DataAgent(tables=)`, manifest keys `{catalog, schema, tables}`, constants `APX_DIR`/`SCHEMA_MANIFEST_NAME` — all consistent across tasks. The `tables` param name (not `schema`) avoids the `DataAgent` positional `schema` collision, noted up front.

**Verify-on-execute:** confirm `_scaffold_model_serving`'s exact signature/`files` dict when adding the manifest there (mirror the `_scaffold_apps` change); confirm the `<style>` f-string in `_ui_chat.py` uses doubled braces `{{ }}`; confirm `col.type_text` is populated for the target SDK version (fallback `''` already handled).
