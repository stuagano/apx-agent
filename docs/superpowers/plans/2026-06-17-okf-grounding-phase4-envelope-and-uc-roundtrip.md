# OKF Grounding Phase 4 — `knowledge =` Envelope Declaration + OKF↔UC Comment Round-Trip

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement **Part A** task-by-task. **Part B requires a design sign-off (governance) before execution** — see its gate.

**Goal:** (A) Let an agent declare its grounding bundle in the envelope — `[tool.apx.agent] knowledge = "./okf"` — so grounding is sourced from an explicit OKF bundle path instead of (only) the cwd-walk. (B) Round-trip curated knowledge between the OKF bundle and Unity Catalog: pull UC `COMMENT`s into OKF descriptions, and push curated OKF descriptions back to UC as `COMMENT`s.

**Architecture:** Part A adds one `AgentConfig` field threaded through `resolve_agent` → the `data`/`coworker` template → `DataAgent`, which gains an explicit-bundle grounding source (highest priority, ahead of the cwd-walk). Reuses Phase-1/2 readers (`okf_manifest`, `okf_grounding`) on an explicit path — no new parsing. Part B adds two CLI commands over the bundle ⇄ UC boundary using the existing SQL warehouse / Tables API, **dry-run by default**, gated on the caller's UC grants.

**Tech Stack:** Python 3.11+, Pydantic (AgentConfig), Click, Databricks SDK (SQL execution / Tables API), pytest. Tests: `cd python && uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-06-16-okf-grounding-design.md` (the GTM §8 "customer owns their grounding" thesis is what Part B operationalizes — UC is the catalog of record; OKF is the portable, enrichable layer; round-trip keeps them coherent).

**Builds on:** Phases 1–3 (`_okf.py` readers/writer/refresh; `load_baked_schema`/`load_okf_grounding`; scaffold/refresh/cache-hook). The runtime grounding path gains ONE new (highest-priority) source in Part A; Part B is new CLI surface only.

---

## Decisions locked for Part A (envelope `knowledge =`)

- **Semantics:** `knowledge` is a path to an **OKF bundle directory** (the dir containing `datasets/` + `tables/`), resolved relative to the project root. Examples: `"./.apx/okf"`, `"./okf"`, `"../shared-knowledge/okf"`. When set, grounding loads **directly** from that bundle (`okf_manifest(path)` + `okf_grounding(path)`) — NOT via the cwd upward-walk.
- **Precedence (new highest-priority baked source):** explicit `tables=` override → live `introspect_schema(ws)` → **`knowledge=` explicit bundle** → cwd-walk `load_baked_schema()` → ungrounded. (The `knowledge` bundle outranks the walk because it is an explicit author declaration.)
- **Totality:** a missing/malformed `knowledge` bundle does **not** crash — it logs a warning and falls through to the cwd-walk (same None-on-error discipline as every other reader).
- **Catalog/schema gate still applies:** the bundle's `catalog`/`schema` must match the template's `catalog`/`schema` (the existing `data_agent.py` byte-match gate), else it degrades — so `knowledge` can't silently ground against the wrong schema.

## Decisions proposed for Part B (UC round-trip) — **NEEDS YOUR SIGN-OFF**

> **GOVERNANCE GATE.** Part B has apx **write to a customer's Unity Catalog metadata** (`COMMENT ON`). That is an outward, stateful action on their catalog of record. Do not implement Part B until the user confirms: (1) we want apx to write UC comments at all; (2) the dry-run-default + explicit-`--apply` posture below; (3) the direction(s) in scope. Part A has no such concern and can ship independently.

- **Two directions, two commands** (proposed):
  - `apx-agent okf pull-comments` — **UC → OKF**: read table/column `COMMENT`s via the Tables API, write them into the bundle's `# Schema` Description cells + a `# Overview` from the table comment. Read-only on UC. Safe.
  - `apx-agent okf push-comments` — **OKF → UC**: write curated OKF descriptions back to UC via `COMMENT ON TABLE` / `ALTER TABLE … ALTER COLUMN … COMMENT`. **Dry-run by default** (prints the SQL it would run); `--apply` actually executes; each statement gated on the caller's `MODIFY`/ownership (surfaces the permission error, never swallows).
- **Conflict policy** (pull): UC comment wins for an empty OKF cell; an already-curated OKF cell is **preserved** unless `--overwrite` (don't clobber human curation with a possibly-staler UC comment).
- **No automatic push.** `push-comments` is always explicit and never wired into deploy/scaffold/refresh.

---

# PART A — `knowledge =` envelope declaration (ready to implement)

## Task A0: Worktree (already created by controller)
Worktree at `/Users/stuart.gano/Documents/apx-agent-okf-phase4` on `impl/okf-grounding-phase4` (off the Phase-3 tip). Verify: `cd python && uv run pytest tests/test_okf.py tests/test_data_agent.py -q` → green.

## Task A1: `knowledge` field on `AgentConfig` + a direct-bundle loader

**Files:** Modify `python/src/apx_agent/_models.py` (add field); modify `python/src/apx_agent/_schema.py` (add `load_grounding_from_path`); Test `python/tests/test_models.py` (or wherever AgentConfig parsing is tested) + `python/tests/test_schema.py`.

- [ ] **Step 1: failing test (test_schema.py)** for a direct-bundle loader that reads grounding from an explicit path (no cwd-walk):

```python
class TestLoadGroundingFromPath:
    def test_reads_manifest_and_grounding_from_explicit_bundle(self, tmp_path):
        from apx_agent._okf import write_okf_bundle
        from apx_agent._schema import load_grounding_from_path
        m = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        write_okf_bundle(m, tmp_path / "okf", timestamp="z")
        manifest, grounding = load_grounding_from_path(tmp_path / "okf")
        assert manifest == m
        assert grounding is None  # bare bundle, no enrichment

    def test_missing_path_returns_none_none(self, tmp_path):
        from apx_agent._schema import load_grounding_from_path
        assert load_grounding_from_path(tmp_path / "nope") == (None, None)
```

- [ ] **Step 2: run, expect FAIL.**
- [ ] **Step 3: implement.** In `_schema.py`:

```python
def load_grounding_from_path(okf_root: "Path | str") -> "tuple[dict | None, dict | None]":
    """Load (manifest, grounding) directly from an explicit OKF bundle dir.

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
```

Add to `_models.py` `AgentConfig` (after `template`):
```python
    knowledge: str | None = None
    """Path to an OKF bundle directory used to ground this agent.

    ``[tool.apx.agent] knowledge = "./.apx/okf"``. When set, grounding loads
    directly from this bundle (highest-priority baked source) instead of the
    cwd upward-walk. Relative to the project root."""
```

- [ ] **Step 4: run** `cd python && uv run pytest tests/test_schema.py -q` + the AgentConfig-parsing test asserting `knowledge` round-trips from TOML. **Step 5: commit.**

## Task A2: thread `knowledge` through `resolve_agent` into the template spec

**Files:** Modify `python/src/apx_agent/_wiring.py` (`resolve_agent`); Test `python/tests/test_wiring.py` (match the existing template-resolution tests).

READ `resolve_agent` (~`_wiring.py:284-295`) and the `data`/`coworker` template `Spec` first. When `config.knowledge` is set and the template spec doesn't already carry it, inject it so the template forwards it to `DataAgent`:

- [ ] **Step 1: failing test** that `resolve_agent` with `config.knowledge="./okf"` and `template={name:"data",...}` passes `knowledge` into `template_registry.build`'s spec (stub the registry to capture the spec).
- [ ] **Step 2–3:** in `resolve_agent`, where `spec = {k: v for k, v in template_dict.items() if k != "name"}` is built, add:
```python
        if config.knowledge is not None and "knowledge" not in spec:
            spec["knowledge"] = config.knowledge
```
- [ ] **Step 4–5:** run wiring tests; commit.

## Task A3: `data`/`coworker` template + `DataAgent` consume `knowledge` as the explicit-bundle source

**Files:** Modify `python/src/apx_agent/data_agent.py` (+ its template Spec); modify `python/src/apx_agent/coworker.py` if its Spec is separate; Test `python/tests/test_data_agent.py`.

READ how the `data` template's `Spec` is defined and how `DataAgent`/`_build_data_tools_and_instructions` receive params. Add a `knowledge: str | None = None` spec field and thread it into the builder.

- [ ] **Step 1: failing tests:**
  - `knowledge=` pointing at an enriched bundle → its enrichment reaches the instructions even when cwd has no `.apx/`.
  - precedence: a `tables=` override still wins over `knowledge=`.
  - a missing `knowledge` path → falls through to cwd-walk / ungrounded without raising.
- [ ] **Step 2–3:** in `_build_data_tools_and_instructions`, add a `knowledge: str | None = None` param and insert the explicit-bundle source in the resolution chain (after live introspect, before `load_baked_schema()`):
```python
    if not resolved_tables and knowledge:
        from ._schema import load_grounding_from_path
        km, kg = load_grounding_from_path(knowledge)
        if km and km.get("catalog") == catalog and km.get("schema") == schema and isinstance(km.get("tables"), dict):
            resolved_tables = km["tables"]
            baked_was_source = True
            explicit_grounding = kg            # used below instead of load_okf_grounding()
```
and when building instructions, prefer `explicit_grounding` when the source was the `knowledge` bundle (else keep the Phase-2 `load_okf_grounding() if baked_was_source` behavior). Thread `knowledge` from the template Spec into this builder.
- [ ] **Step 4:** run `cd python && uv run pytest tests/test_data_agent.py tests/test_coworker.py tests/test_schema.py -q` → green. **Step 5: commit.**

## Task A4: scaffold/docs emit `knowledge` (optional, low-risk)

**Files:** `python/src/apx_agent/_project_gen.py` (so generated `pyproject.toml` includes `knowledge = "./.apx/okf"` when scaffolding emits a bundle); `docs/reference/configuration.md`. Test: scaffold-yaml test asserts the key is emitted. Keep this minimal; commit separately.

## Task A5: Part-A integration verification
- [ ] Full suite green; a smoke that builds the payroll agent with `[tool.apx.agent] knowledge = "./.apx/okf"` and confirms the enriched prompt loads from the declared bundle. Commit (or verification-only).

---

# PART B — OKF ↔ UC comment round-trip (**gated on governance sign-off**)

> Do not start Part B until the user signs off on the governance gate above. Tasks are outlined; expand to full TDD steps after sign-off.

## Task B1: `okf pull-comments` (UC → OKF, read-only)
- New `_okf.py` helper `apply_uc_comments(okf_root, comments, *, overwrite=False)` where `comments = {table: {"_table": str, col: str}}`: fills empty `# Schema` Description cells (and a `# Overview` from the table comment) without clobbering curated cells unless `overwrite`.
- New CLI `apx-agent okf pull-comments [--profile] [--overwrite]`: reads the bundle's catalog/schema, fetches comments via the Tables API (`ws.tables.get(...).columns[].comment`, `.comment`), calls `apply_uc_comments`, regenerates the cache. Read-only on UC. TDD with a stubbed `ws`.

## Task B2: `okf push-comments` (OKF → UC, dry-run default, `--apply`)
- New `_okf.py` helper `uc_comment_statements(okf_root) -> list[str]`: emits `COMMENT ON TABLE <fqn> IS '...'` and `ALTER TABLE <fqn> ALTER COLUMN <col> COMMENT '...'` for every non-empty OKF description (properly escaped). Pure/SQL-string-building — unit-testable with no workspace.
- New CLI `apx-agent okf push-comments [--profile] [--apply]`: prints the statements (dry-run); with `--apply`, executes each via the SQL warehouse, reporting per-statement success/permission-error (never swallowed). Gated on the caller's `MODIFY`/ownership.

## Task B3: round-trip integration
- Pull → enrich → push (dry-run) on a stubbed/test schema; assert the generated SQL matches the curated descriptions and that pull is non-destructive to curated cells.

---

## Self-review notes
- **Part A** is independent of Part B and ships first; it adds exactly one new (highest-priority) grounding source, totalised, behind the existing catalog/schema gate — the Phase-1/2 runtime behavior is otherwise unchanged (no `knowledge` set → identical to today).
- **Part B** is new CLI surface that writes to UC only under explicit `--apply`; dry-run is the default; pull is read-only and non-destructive to curated cells.
- **Governance** is the one real gate — flagged for the user before any `COMMENT ON` write is implemented.
- **Deferred beyond Phase 4:** lineage-aware grounding (join hints from UC FK metadata); multi-bundle composition; `okf_version` migration tooling.
