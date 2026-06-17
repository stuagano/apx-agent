# OKF Grounding Phase 3 — Bundle Lifecycle (scaffold · refresh · cache hook)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Complete the OKF bundle lifecycle in the CLI so OKF is the substrate end-to-end: new projects scaffold an OKF bundle (not just a `schema.json`), `refresh-schema` updates the `# Schema` tables **without destroying enriched bodies**, and a pre-commit hook keeps the derived `schema.json` cache in sync with the bundle.

**Architecture:** Three independent CLI-lifecycle changes, each reusing the Phase-1/2 primitives in `_okf.py` (`write_okf_bundle`, `okf_manifest`, `OKFDocument`, `_schema_row`, `_schema_rows_with_desc`, `parse_schema_columns`). New `_okf.py` helpers: `_replace_section` and `refresh_okf_schema` (preserves `# Overview`/`# Joins`/`# Examples` AND carries over per-column descriptions on re-introspect). No changes to the runtime grounding path (Phases 1–2) — this is authoring/lifecycle only.

**Tech Stack:** Python 3.11+, Click, pytest, pre-commit. Tests: `cd python && uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-06-16-okf-grounding-design.md` §7 (scaffold emits OKF), Q1 (refresh preserves enriched bodies — **decided: yes**), Q2 (cache regen — **decided: build-step is already covered by scaffold/migrate/refresh; add a pre-commit hook as the convenience guard**).

**Decisions locked for this plan (the spec's open questions, resolved):**
- **Q1 = preserve.** `refresh-schema` updates only each table's `# Schema` columns/types; it keeps `# Overview`/`# Joins`/`# Examples` verbatim and **carries over a column's Description cell** when that column still exists (re-introspection doesn't supply descriptions, so a naive rewrite would wipe them — it must merge).
- **Q2 = pre-commit auto-fix hook** that regenerates `.apx/schema.json` from a changed `.apx/okf/` bundle (mirrors the existing `uv-lock-registry --fix` hook pattern). The build-step path already regenerates the cache (scaffold/migrate/refresh write it).
- Removed-table handling on refresh: a table concept whose table no longer exists in the live schema is **deleted** from the bundle (refresh reflects live schema). Logged.

**Invariant across all tasks:** the runtime grounding behavior (Phases 1–2) is unchanged; these only change what the CLI writes to disk. Every existing test stays green.

---

## Task 0: Worktree (already created by controller)

Worktree at `/Users/stuart.gano/Documents/apx-agent-okf-phase3` on branch `impl/okf-grounding-phase3` (off the Phase-2 tip). Verify baseline:

- [ ] **Step 1:** `cd python && uv run pytest tests/test_okf.py tests/test_cli.py -q` → expect green.

---

## Task 1: Scaffold emits an OKF bundle for new projects

**Files:** Modify `python/src/apx_agent/cli.py` (`_scaffold_model_serving` ~1880, `_scaffold_apps` ~2039 — both write `files[".apx/schema.json"]` then materialize a `files` dict under base `target`); Test `python/tests/test_cli_scaffold_yaml.py` (or `test_cli.py` — match where scaffold tests live).

Add one shared helper and call it from both archetypes after the file-write loop, so a new project ships `.apx/okf/` (source of truth) **and** `.apx/schema.json` (derived cache, already written).

- [ ] **Step 1: Write the failing test** (read the existing scaffold tests first to reuse their invocation harness; they call the scaffold via `CliRunner` or the `_scaffold_*` helpers). Add a test that scaffolds a project with a stubbed manifest and asserts the OKF bundle exists and round-trips:

```python
class TestScaffoldEmitsOKF:
    def test_scaffold_writes_okf_bundle_and_cache(self, tmp_path, monkeypatch):
        import json
        from apx_agent import cli
        from apx_agent._okf import okf_manifest

        # Stub introspection so the scaffold has a manifest without a live workspace.
        manifest = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        monkeypatch.setattr(cli, "_schema_manifest_for_scaffold", lambda *a, **k: manifest)

        target = tmp_path / "proj"
        cli._scaffold_model_serving(  # adapt to the real signature you find
            name="proj", target=target, catalog="c", schema="s", force=False,
        )
        assert (target / ".apx" / "okf" / "datasets" / "s.md").is_file()
        assert (target / ".apx" / "schema.json").is_file()
        # OKF bundle round-trips to the same manifest the cache holds
        assert okf_manifest(target / ".apx" / "okf") == json.loads((target / ".apx" / "schema.json").read_text())
```

NOTE: read the real `_scaffold_model_serving` signature and call it correctly; if it's not directly callable in a test (requires a wizard/IO), instead drive the public `scaffold` Click command via `CliRunner` with `--catalog/--schema` and the manifest stub, and assert the same files. Pick whichever the existing scaffold tests already do.

- [ ] **Step 2: Run, expect FAIL** — the `.apx/okf/` dir is not created yet.

- [ ] **Step 3: Implement.** Add a module-level helper near the scaffold helpers:

```python
def _write_okf_bundle_for_scaffold(target: Path, manifest: dict, *, force: bool) -> None:
    """Write the OKF bundle (source of truth) next to the derived schema.json cache."""
    from datetime import datetime, timezone
    from ._okf import write_okf_bundle

    okf_root = target / ".apx" / "okf"
    if okf_root.exists() and not force:
        click.echo(f"  skip   {okf_root} (exists; pass --force to overwrite)")
        return
    write_okf_bundle(manifest, okf_root, timestamp=datetime.now(timezone.utc).isoformat())
    click.echo(f"  write  {okf_root} (OKF bundle)")
```

Then in **both** `_scaffold_model_serving` and `_scaffold_apps`, immediately after the `for rel_path, content in files.items():` write loop, add:

```python
    if manifest is not None:
        _write_okf_bundle_for_scaffold(target, manifest, force=force)
```

(The `.apx/schema.json` line is unchanged — it stays as the committed derived cache. `target` and `force` are already in scope in both functions.)

- [ ] **Step 4: Run, expect PASS** — the new test + the full `cd python && uv run pytest tests/test_cli.py tests/test_cli_scaffold_yaml.py -q`.

- [ ] **Step 5: Commit**
```bash
git add python/src/apx_agent/cli.py python/tests/test_cli_scaffold_yaml.py
git commit -m "feat(cli): scaffold emits an OKF bundle for new projects"
```

---

## Task 2: `refresh-schema` preserves enriched bodies (spec Q1)

**Files:** Modify `python/src/apx_agent/_okf.py` (add `_replace_section`, `refresh_okf_schema`); modify `python/src/apx_agent/cli.py` (`refresh_schema` ~2491); Test `python/tests/test_okf.py` + `python/tests/test_cli.py`.

When a `.apx/okf/` bundle exists, `refresh-schema` must update each table's `# Schema` (columns/types from live introspection) while **preserving** `# Overview`/`# Joins`/`# Examples` and **carrying over each surviving column's Description cell** (re-introspection supplies no descriptions). Then regenerate the `schema.json` cache. When no bundle exists, keep today's behavior (write `schema.json`).

- [ ] **Step 1: Write failing tests for the `_okf.py` core (append to test_okf.py)**

```python
class TestRefreshOKFSchema:
    def _enriched_bundle(self, root):
        from apx_agent._okf import write_okf_bundle
        m = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(6,2))", "old_col(int)"]}}
        write_okf_bundle(m, root, timestamp="z")
        (root / "tables" / "pay_runs.md").write_text(
            "---\ntype: Unity Catalog Table\ntitle: pay_runs\ndescription: d\ntimestamp: z\n---\n\n"
            "# Overview\nPay records.\n\n"
            "# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `gross_pay` | decimal(6,2) | Gross before deductions. |\n"
            "| `old_col` | int | Going away. |\n\n"
            "# Joins\nJoin employees on `employee_id`.\n"
        )
        return root

    def test_refresh_updates_schema_preserves_bodies_and_col_desc(self, tmp_path):
        from apx_agent._okf import refresh_okf_schema, OKFDocument, okf_manifest
        okf = self._enriched_bundle(tmp_path / "okf")
        # live schema: gross_pay type widened, old_col dropped, net_pay added
        new = {"catalog": "c", "schema": "s", "tables": {
            "pay_runs": ["gross_pay(decimal(10,2))", "net_pay(decimal(10,2))"],
        }}
        refresh_okf_schema(okf, new, timestamp="z2")
        doc = OKFDocument.parse((okf / "tables" / "pay_runs.md").read_text())
        # bodies preserved
        assert "# Overview" in doc.body and "Pay records." in doc.body
        assert "Join employees on `employee_id`." in doc.body
        # schema updated: new type, dropped col gone, new col present
        assert "decimal(10,2)" in doc.body
        assert "old_col" not in doc.body
        assert "net_pay" in doc.body
        # surviving column's description carried over
        assert "Gross before deductions." in doc.body
        # manifest reflects the new columns
        assert okf_manifest(okf)["tables"]["pay_runs"] == ["gross_pay(decimal(10,2))", "net_pay(decimal(10,2))"]

    def test_refresh_removes_dropped_table(self, tmp_path):
        from apx_agent._okf import write_okf_bundle, refresh_okf_schema
        m = {"catalog": "c", "schema": "s", "tables": {"a": ["x(int)"], "b": ["y(int)"]}}
        okf = tmp_path / "okf"
        write_okf_bundle(m, okf, timestamp="z")
        refresh_okf_schema(okf, {"catalog": "c", "schema": "s", "tables": {"a": ["x(int)"]}}, timestamp="z2")
        assert (okf / "tables" / "a.md").is_file()
        assert not (okf / "tables" / "b.md").exists()
```

- [ ] **Step 2: Run, expect FAIL** — `cd python && uv run pytest tests/test_okf.py::TestRefreshOKFSchema -q`.

- [ ] **Step 3: Implement the `_okf.py` core.** Append:

```python
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


def refresh_okf_schema(okf_root: "Path | str", manifest: dict, *, timestamp: str) -> None:
    """Update an OKF bundle's ``# Schema`` tables to match ``manifest`` while
    preserving enriched bodies and per-column descriptions. Adds new tables,
    removes table concepts not in the manifest, refreshes the dataset concept and
    ``tables/index.md``. Caller regenerates the ``schema.json`` cache afterwards.
    """
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

    # Remove concepts whose table no longer exists in the live schema.
    for p in tdir.glob("*.md"):
        if p.name in _RESERVED:
            continue
        stem_name = OKFDocument.parse(p.read_text()).frontmatter.get("title") or p.stem
        if stem_name not in tables:
            p.unlink()

    # Refresh dataset concept body (# Tables) + tables/index.md to the new set.
    ds_path = root / "datasets" / f"{schema}.md"
    if ds_path.is_file():
        ds = OKFDocument.parse(ds_path.read_text())
        ds.frontmatter["timestamp"] = timestamp
        ds.body = "# Tables\n" + "".join(f"* [{t}](../tables/{t}.md)\n" for t in tables)
        ds_path.write_text(ds.serialize())
    (tdir / "index.md").write_text("# Tables\n" + "".join(f"* [{t}]({t}.md)\n" for t in tables))
```

- [ ] **Step 4: Run the `_okf.py` core tests, expect PASS** — `cd python && uv run pytest tests/test_okf.py -q`.

- [ ] **Step 5: Wire into the CLI `refresh_schema` command.** In `cli.py`, after `manifest` is computed and validated (before/instead of the unconditional `out.write_text(...)`), branch on bundle presence:

```python
    apx = Path.cwd() / APX_DIR
    okf_root = apx / "okf"
    if okf_root.is_dir():
        from datetime import datetime, timezone
        from ._okf import refresh_okf_schema, okf_manifest
        refresh_okf_schema(okf_root, manifest, timestamp=datetime.now(timezone.utc).isoformat())
        regen = okf_manifest(okf_root)
        if regen is not None:
            (apx / SCHEMA_MANIFEST_NAME).write_text(_json.dumps(regen, indent=2))
        n = len(regen["tables"]) if regen else 0
        click.echo(f"refreshed {okf_root} (+ schema.json cache) — {n} tables from {catalog}.{schema}")
        return
    # ── no bundle: legacy schema.json path (unchanged) ──
    out = Path.cwd() / APX_DIR / SCHEMA_MANIFEST_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_json.dumps(manifest, indent=2))
    ...
```

- [ ] **Step 6: Add a CLI test (append to test_cli.py)** that runs `refresh-schema` on a project with an enriched OKF bundle (stub `_schema_manifest_for_scaffold` to return updated columns), asserting the enriched body survives and the cache updated. Mirror the harness used by the existing `migrate-to-okf` CLI tests.

- [ ] **Step 7: Run** — `cd python && uv run pytest tests/test_okf.py tests/test_cli.py -q` → expect green.

- [ ] **Step 8: Commit**
```bash
git add python/src/apx_agent/_okf.py python/src/apx_agent/cli.py python/tests/test_okf.py python/tests/test_cli.py
git commit -m "feat(cli): refresh-schema updates # Schema preserving enriched OKF bodies"
```

---

## Task 3: Pre-commit hook keeps `schema.json` in sync with the bundle (spec Q2)

**Files:** Create `scripts/regen-okf-cache.py`; modify `.pre-commit-config.yaml`; Test `python/tests/test_okf_cache_hook.py`.

A local auto-fix hook (mirroring `uv-lock-registry --fix`): when any `.apx/okf/**/*.md` changes, regenerate the sibling `.apx/schema.json` from the bundle so the committed cache never drifts from its source.

- [ ] **Step 1: Write a failing test for the regen script's core (`python/tests/test_okf_cache_hook.py`)**

```python
"""The cache-regen helper rewrites .apx/schema.json from a changed .apx/okf bundle."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_regen_writes_cache_from_bundle(tmp_path):
    from apx_agent._okf import write_okf_bundle
    m = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
    apx = tmp_path / ".apx"
    write_okf_bundle(m, apx / "okf", timestamp="z")
    (apx / "schema.json").write_text("{}")  # stale cache

    script = Path(__file__).resolve().parents[2] / "scripts" / "regen-okf-cache.py"
    r = subprocess.run([sys.executable, str(script), str(apx / "okf" / "tables" / "t.md")],
                       capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode in (0, 1), r.stderr  # 0 = no change, 1 = rewrote (pre-commit convention)
    assert json.loads((apx / "schema.json").read_text())["tables"] == {"t": ["a(int)"]}
```

- [ ] **Step 2: Run, expect FAIL** — script does not exist.

- [ ] **Step 3: Create `scripts/regen-okf-cache.py`**

```python
#!/usr/bin/env python3
"""Pre-commit auto-fix: regenerate each changed .apx/okf bundle's sibling
schema.json cache so the committed cache never drifts from its source.

Invoked by pre-commit with the changed .apx/okf/**/*.md paths as argv. Exits 1
(and re-stages) when it rewrote a cache, 0 when everything was already in sync —
the pre-commit "fixer" convention.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    # Resolve apx_agent from the repo's python/src without installing.
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "python" / "src"))
    from apx_agent._okf import okf_manifest

    changed = False
    seen: set[Path] = set()
    for arg in argv:
        p = Path(arg).resolve()
        # find the enclosing .apx/okf dir
        okf_root = next((a for a in [p, *p.parents] if a.name == "okf" and a.parent.name == ".apx"), None)
        if okf_root is None or okf_root in seen:
            continue
        seen.add(okf_root)
        manifest = okf_manifest(okf_root)
        if manifest is None:
            continue
        cache = okf_root.parent / "schema.json"
        new = json.dumps(manifest, indent=2)
        if not cache.is_file() or cache.read_text() != new:
            cache.write_text(new)
            print(f"regenerated {cache}")
            changed = True
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Add the hook to `.pre-commit-config.yaml`** (in the `repo: local` hooks list, alongside `uv-lock-registry`):

```yaml
      - id: okf-cache-sync
        name: OKF bundle -> schema.json cache in sync (auto-fix)
        entry: python3 scripts/regen-okf-cache.py
        language: system
        files: '\.apx/okf/.*\.md$'
```

- [ ] **Step 5: Run** — `cd python && uv run pytest tests/test_okf_cache_hook.py -q` → expect PASS. Also `chmod +x scripts/regen-okf-cache.py`.

- [ ] **Step 6: Commit**
```bash
git add scripts/regen-okf-cache.py .pre-commit-config.yaml python/tests/test_okf_cache_hook.py
git commit -m "feat(hooks): pre-commit auto-fix keeps schema.json in sync with the OKF bundle"
```

---

## Task 4: Integration verification + payroll refresh smoke

**Files:** none new (verification only); optionally re-run `migrate`/`refresh` on payroll-coworker to confirm the lifecycle composes.

- [ ] **Step 1: Full suite** — `cd python && uv run pytest -q 2>&1 | grep -E "passed|failed" | tail -2` → expect 0 failures.
- [ ] **Step 2: Lifecycle smoke (EXECUTE + INSPECT)** on the committed payroll bundle, in a tmp copy so nothing is mutated in git:
```bash
cd /Users/stuart.gano/Documents/apx-agent-okf-phase3/python
uv run python - <<'PY'
import shutil, tempfile, pathlib, json
from apx_agent._okf import okf_manifest, okf_grounding, refresh_okf_schema
src = pathlib.Path("payroll-coworker/.apx")
tmp = pathlib.Path(tempfile.mkdtemp()) / ".apx"
shutil.copytree(src, tmp)
before_g = okf_grounding(tmp / "okf")           # enrichment present (pay_runs)
m = okf_manifest(tmp / "okf")
refresh_okf_schema(tmp / "okf", m, timestamp="z")  # refresh with same schema
after_g = okf_grounding(tmp / "okf")
assert after_g is not None and "pay_runs" in after_g, "refresh wiped enrichment!"
assert okf_manifest(tmp / "okf") == m, "refresh changed the manifest!"
print("OK: refresh preserved enrichment AND kept the manifest stable")
PY
```
Expected `OK: ...`. Confirms refresh is body-preserving and manifest-stable on real enriched content.
- [ ] **Step 3:** No commit (verification only) — or commit a note if the smoke surfaced anything.

---

## Self-review notes

- **Spec coverage:** §7 scaffold-emits-OKF → Task 1; Q1 refresh-preserves-bodies (+ column-desc carryover, the subtle part) → Task 2; Q2 cache-sync hook → Task 3.
- **The runtime grounding path (Phases 1–2) is untouched** — Tasks change only what the CLI writes. Existing tests stay green (regression check in Task 4 Step 1).
- **The dangerous edge** (refresh wiping enrichment) is pinned by Task 2 `test_refresh_updates_schema_preserves_bodies_and_col_desc` and Task 4 Step 2 on the real payroll bundle.
- **Name consistency:** `_write_okf_bundle_for_scaffold`, `_replace_section`, `_schema_block_md`, `refresh_okf_schema`, `regen-okf-cache.py` / `okf-cache-sync`.
- **Deferred beyond Phase 3:** declaring `knowledge = "./okf/"` in the `[tool.apx.agent]` envelope; OKF↔UC round-trip (writing curated bundle prose back to UC `COMMENT`s); `okf_version` bump handling.
