# OKF as apx-agent's Grounding Substrate — Design

> Status: **Design spec (approved-decisions baked in).** Date: 2026-06-16.
> Deliverable scope: **this document is the spec only.** The implementation spike (§9) is a separate, gated next step the user approves *after* this spec.
> Spike target: `python/payroll-coworker/` (already ships `.apx/schema.json`).

---

## 1. Summary & locked decisions

apx-agent grounds a `DataAgent`/`CoworkerAgent` against a Unity Catalog schema by baking a small manifest, `.apx/schema.json`, that the agent reads at construction time. This design **replaces that manifest's role** with an [Open Knowledge Format (OKF) v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog) bundle under `.apx/okf/`, while keeping every downstream caller byte-for-byte unchanged in Phase 1.

### The approach in one line

Make `.apx/okf/` an OKF v0.1 bundle the single source of truth: teach `load_baked_schema()` to **prefer** the bundle and return the **byte-identical** `{catalog, schema, tables}` dict (Phase 1, zero downstream change), with `.apx/schema.json` demoted to a committed dual-read derived cache; in Phase 2 a **separate** `load_okf_grounding()` accessor feeds enriched `# Schema`/`# Joins`/`# Examples` bodies into the prompt via a **new, default-identical `grounding=` param** on `build_instructions_from_schema` — never via the `instructions=` override (which would silently collide with user-supplied instructions).

### Locked decisions (do not relitigate)

1. **Role = FULL REPLACEMENT.** The OKF bundle is the single grounding substrate. `.apx/schema.json` becomes a **derived cache** (generated from OKF), not the source of truth. The OKF parse flows through the existing `load_baked_schema()` seam, so all downstream callers (`uc_table` resources, function wiring, `build_instructions_from_schema`) are unchanged.
2. **Authoring = BOTH.** Scaffold auto-generates the OKF bundle from UC metadata (UC Tables API, like `schema.json` today); humans/agents then enrich the markdown bodies (semantics, example queries, joins). Bundle is diffable + version-controlled.
3. **Deliverable = spec + (later) spike.** This document is the **spec only**. The spike is a separate gated step after the user approves the spec.
4. **GTM = first-class.** Databricks positioning is a core section (§8).

### Verification fixes folded in (resolved here, not deferred)

Three adversarial review lenses returned `sound-with-fixes`. Every blocking issue is resolved in this spec:

| # | Blocking issue (verified against source) | Resolution (where) |
|---|---|---|
| F1 | **Loader can crash** — OKF branch had no `try/except`; `yaml.safe_load`, raw `fm["catalog"]` subscripts, and a mangled pipe row can raise out of `load_baked_schema`, turning today's graceful ungrounding into an **app-boot crash in the deployed container**. Violates OKF §9 permissive-consumption *and* the function's own None-on-error contract. | §4 (loader): the entire OKF branch is wrapped in `try/except Exception -> warning + fall through`; `_load_okf_schema` uses `fm.get(...)`, returns `None` on any miss. |
| F2 | **Pipe header/separator rows inject phantom columns** — naive `split('|')` emits `Column(Type)` and `---(---)` as bogus columns, polluting the order-sensitive prompt-equality gate. | §5 (parser): `_parse_schema_table` requires a **backticked col-1 token**, dropping the un-backticked header and `---` separator. |
| F3 | **`sorted(tables/*.md)` includes `index.md`** → phantom `index` table → phantom `uc_table` ResourceSpec + prompt line. | §4/§5: ordering excludes reserved `index.md`/`log.md`; `sorted()` is the **primary** deterministic order, `tables/index.md` is **advisory override only**. |
| F4 | **Enumeration source ambiguity** — the dataset's human-curated `# Tables` body lists only 2 business tables; enumerating from it silently drops the 3 infra tables (the "5-table trap"). | §4: enumeration is **always** the `tables/` directory (`sorted()`, index.md advisory), **never** the dataset `# Tables` list. |
| F5 | **`OKFDocument.validate()` on the read path** would reject third-party bundles missing `title`/`timestamp` (§9 only requires non-empty `type`). | §3/§4: `validate()` is **emit/producer-side only**, pinned as an invariant; the read path uses `.parse` exclusively. |
| F6 | **Over-claimed parse robustness** — the real ga4 `# Schema` is a `##`/`###` RECORD hierarchy with 4-space-indented dot-path bullets, not a flat bullet list; several concepts have no `# Schema` at all. | §5: claim scoped honestly — apx **emits** pipe tables (owned, round-trippable); ga4-bullet handling is **best-effort lossy**; `_parse_schema_table` returns `[]` (never raises) when `# Schema` is absent, preserving the load-bearing table name. |
| F7 | **Phase-2 infra-exclusion breaks "default-identical"** — excluding the 3 infra tables from the grounded prompt block makes the payroll prompt silently drop 3 tables the moment *any* enrichment lands. | §6: **keep all tables** in `_format_grounded_schema_block` (option a) so the grounded path stays prompt-identical to Phase 1 for un-enriched tables; infra exclusion is explicitly **not** shipped. |
| F8 | **Deploy-copy divergence** — `cli.py:1113` scaffold heredoc copies `.apx-agent` but **not** `.apx`; new projects ship no bundle → ungrounded in prod, passes locally. payroll's `databricks.yml:45` is hand-patched and already correct. | §7/§9: mandatory template fix `cp -r .apx .build/ 2>/dev/null || true` + a read-after-deploy `caps` check that `./.build/.apx/okf/` is present. |
| F9 | **`\|` in a future column comment** shifts the Type cell and corrupts the type once `.comment` is captured. | §5: escape `\|` (or strip newlines/pipes) on **emit**. |
| F10 | **Multiple `datasets/*.md`** → non-deterministic catalog/schema carrier → intermittent ungrounding via the byte-match gate. | §4: pin to a single dataset concept; deterministic precedence (`sorted()` first) if more than one. |

---

## 2. Background: today's `schema.json` grounding

Verified by reading `python/src/apx_agent/_schema.py` and `python/src/apx_agent/data_agent.py`.

### The manifest shape (`.apx/schema.json`)

```json
{ "catalog": "<str>", "schema": "<str>", "tables": { "<table>": ["<col>(<type>)", ...] } }
```

Real column-string examples from `python/payroll-coworker/.apx/schema.json` (the spike target): `"employee_id(string)"`, `"gross_pay(decimal(6,2))"`, `"hire_date(date)"`, `"tags(array<string>)"`, `"embedding(array<float>)"`, `"created_at(double)"`, `"overtime_hours(int)"`.

### `python/src/apx_agent/_schema.py` (the seam)

- **`load_baked_schema(start=None) -> dict | None`** — walks **up** from cwd to the filesystem root, returns the first `.apx/schema.json` parsed as a dict (`{catalog, schema, tables}`). **Total**: wraps `json.loads` in `try/except -> return None`; returns `None` (not crash) on miss/parse-error so callers degrade to ungrounded. **This is the seam to keep — including its totality.**
- `introspect_schema(ws, catalog, schema, warehouse_id) -> {table: [col(type)]}` — via `information_schema.columns` (needs a SQL warehouse).
- `introspect_schema_columns(ws, catalog, schema) -> {table: [col(type)]}` — same shape via the **UC Tables API** (no warehouse); used at **scaffold** time.
- `build_instructions_from_schema(catalog, schema, tables, persona, objective) -> str` — if `tables` known: renders the "you already know the schema, do NOT run `SHOW TABLES`" framing + `_format_schema_block` (bounded `- table: col(type), ... (+N more)`, **max 12 cols / 20 tables**). If empty: renders a discovery prompt.

### `python/src/apx_agent/data_agent.py` resolution priority (build path)

```
resolved_tables = (tables= explicit override) OR introspect_schema(ws, ...) OR load_baked_schema()
```

Then, with resolved tables:

- **Each table name → `ResourceSpec("uc_table", f"{catalog}.{schema}.{t}")`** (`data_agent.py:117`) — governed resources on the SQL tool. **Table names are load-bearing, not just prose.**
- `uc_function_toolkit(f"{catalog}.{schema}", ws=ws)` (`:124`) wires UC functions as tools. Uses `tables.keys()` only.
- `resolved_instructions = instructions= OR build_instructions_from_schema(...)` (`:132`).
- A catalog/schema **byte-match gate** (`data_agent.py:80–84`): baked `catalog`/`schema` must equal `[tool.apx.agent.data]` config; on mismatch the agent drops to ungrounded. The gate checks `isinstance(tables, dict)` — **not** column content.

Three callers funnel through `load_baked_schema` (grep-verified): `data_agent.py:79` (build), `_wiring.py:431` (stuffs the result into `AgentContext.schema` → landing card / dev UI), `cli.py:2500` (`refresh-schema` reads existing to learn catalog/schema). **No code reads `.apx/schema.json` via raw I/O.**

**Consequence for any OKF loader:** it MUST still yield `{catalog, schema, tables:{name:[col(type)]}}` reliably — table names drive resources + function wiring, not only the prompt.

---

## 3. OKF v0.1 in one screen (what apx relies on)

OKF v0.1 (Draft), Apache-2.0, `GoogleCloudPlatform/knowledge-catalog/okf` — `SPEC.md` + Python reference impl (`src/enrichment_agent/bundle/`) + example bundles (`ga4`, `stackoverflow`, `crypto_bitcoin`).

- **Bundle = a directory tree of markdown files.** One concept = one `.md` doc. Concept ID = file path minus `.md` (`tables/users.md` → `tables/users`).
- **Frontmatter** (YAML, `---`-delimited): **REQUIRED `type`** (non-empty string, e.g. `"Unity Catalog Table"`; not centrally registered). Recommended: `title`, `description`, `resource`, `tags`, `timestamp` (ISO-8601). Producers MAY add **any** custom keys; consumers MUST preserve unknown keys + tolerate unknown types.
- **Body = markdown.** Conventional headings (SHOULD when applicable): `# Schema` = a markdown table `| Column | Type | Description |` (column names in backticks, FKs as markdown links); `# Examples` = fenced code blocks; `# Citations` = numbered sources. Other prose headings (e.g. `# Joins`) are free-form. **`# Schema` is a *convention*, not a guaranteed structure** — the real ga4 corpus uses a `##`/`###` RECORD hierarchy of bullets, not a pipe table.
- **Reserved filenames:** `index.md` (§6 progressive-disclosure listing; NO frontmatter **except** the bundle-root `index.md` MAY carry `okf_version: "0.1"`); `log.md` (§7 change history). The real ga4 bundle omits `okf_version` entirely and has no `log.md` — both are optional.
- **Cross-links:** bundle-relative `/tables/x.md` (recommended) or relative `./x.md`. Untyped edges; relationship kind lives in surrounding prose. **Broken links MUST be tolerated.**
- **Conformance (§9):** a bundle conforms if every non-reserved `.md` has parseable YAML frontmatter with a non-empty `type`. **Consumers MUST NOT reject on** missing optional fields, unknown types, unknown keys, broken links, or missing `index.md`. **Permissive-consumption is core.**
- **Versioning (§11):** minor = backward-compatible additions; major = breaking; declared via `okf_version` in the root `index.md`.

### Reference parser reality (drives the vendor decision)

- **Not on PyPI.** Dist name `enrichment-agent`, import `enrichment_agent`; depending on it drags in `google-adk`, `google-cloud-bigquery`, `markdownify` — fatal for apx's pure-Python packaging.
- Public parsing surface is tiny — `bundle/document.py`:
  - `REQUIRED_FRONTMATTER_KEYS = ("type", "title", "description", "timestamp")`
  - `OKFDocument.parse(text)` — `---`-delimited `yaml.safe_load` split + body `str`.
  - `OKFDocument.serialize()` — `yaml.safe_dump(sort_keys=False)` + body; round-trip stable.
  - `OKFDocument.validate()` — **raises** on any missing `REQUIRED_FRONTMATTER_KEY`.
- **There is no `# Schema`-table-to-columns parser.** The only `# Schema`-aware code (`tools/bundle_tools.py`) is a *name-only* guard: `_schema_field_names(body) -> set[str]` via `re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")`. No types, no order. apx must write the column parser itself.

> **PINNED INVARIANT (F5):** `OKFDocument.validate()` is **producer/emit-side only**. It is **never** called on the consume/read path. The reference `validate()` raises on missing `title`/`timestamp`, but §9 mandates only a non-empty `type`; calling it on read would reject conformant third-party bundles. The read path uses `.parse` exclusively. A future edit must not move `validate()` onto the read path.

> **DECISION — VENDOR, do not depend.** Vendor `python/src/apx_agent/_okf.py`: replicate `OKFDocument.parse/serialize/validate` **exactly** (same `REQUIRED_FRONTMATTER_KEYS`, same `---`/`yaml.safe_load` round-trip), and add the missing `# Schema`-table-to-columns parser apx needs. apx already depends on PyYAML transitively. Pin to OKF v0.1 SPEC §4 with a citing comment; re-check on `okf_version` bumps.

---

## 4. Architecture: bundle layout, the seam, the derived cache

### 4.1 Bundle layout — `.apx/okf/` for payroll-coworker

`HOME = .apx/okf/` (sibling of the demoted `.apx/schema.json`), so `load_baked_schema`'s existing upward walk finds it via the same `.apx/` anchor, and it ships to the deployed App through the **same** `cp -r .apx .build/` line that ships the cache (`databricks.yml:45`, already present for payroll).

Verified for the spike: `.apx/schema.json` has **five** tables — `agent_memory`, `apx_payroll_coworker_memory`, `apx_payroll_coworker_sessions`, `employees`, `pay_runs` — all in **alphabetical** order. `catalog = serverless_stable_qh44kx_catalog`, `schema = payroll_demo`. **All five must carry over**: each becomes a `uc_table` ResourceSpec at `data_agent.py:117`.

```
.apx/
├── schema.json                       # DERIVED CACHE (committed; regenerated from okf/; dual-read fallback). NEVER hand-edited.
└── okf/
    ├── index.md                      # bundle-root index; ONLY place frontmatter is allowed -> carries okf_version: "0.1".
    │                                 # Body = "# Subdirectories" + `*` bullet links. (Spec-valid even if omitted; ga4 omits it. We emit it.)
    ├── datasets/
    │   ├── index.md                  # plain markdown, NO frontmatter
    │   └── payroll_demo.md           # *** CATALOG/SCHEMA CARRIER *** type: "Databricks Schema"; custom frontmatter keys catalog: + schema:.
    │                                 # Body: "# Tables" bullet links (HUMAN-CURATED, business-only — NOT the enumeration source).
    └── tables/
        ├── index.md                  # ADVISORY ordering override; lists the 5 tables. Primary order = sorted() of tables/*.md.
        ├── agent_memory.md           # type: "Unity Catalog Table"; "# Schema" pipe table only (infra; minimal)
        ├── apx_payroll_coworker_memory.md     # infra; minimal
        ├── apx_payroll_coworker_sessions.md   # infra; minimal
        ├── employees.md              # business; "# Schema" pipe table + Phase-2 enrichment slots
        └── pay_runs.md               # business; "# Schema" + "# Joins" (employee_id -> /tables/employees.md) + "# Examples"
```

**DECISION (frontmatter key names):** the dataset concept carries `catalog:` + `schema:` (NOT `schema_name`, NOT `uc_catalog`/`uc_schema`). Rationale: matches `schema.json`'s own field names exactly (least surprise), and their **values must byte-match** `[tool.apx.agent.data]` because of the gate at `data_agent.py:80–84`. These are read from **explicit frontmatter keys**, never parsed out of the `resource:` URI (URI parsing is the brittle path that silently un-grounds).

`datasets/payroll_demo.md` frontmatter (all 4 `REQUIRED_FRONTMATTER_KEYS` + the 2 custom source-of-truth keys):

```yaml
---
type: Databricks Schema
title: payroll_demo
description: Payroll demo schema for the payroll-coworker agent.
resource: https://<host>/explore/data/serverless_stable_qh44kx_catalog/payroll_demo
catalog: serverless_stable_qh44kx_catalog
schema: payroll_demo
timestamp: '2026-06-16T00:00:00+00:00'
tags: [payroll, demo, databricks]
---
# Tables
* [employees](../tables/employees.md) - Employee roster.
* [pay_runs](../tables/pay_runs.md) - Per-period pay records.
```

> **F4 — the `# Tables` body is HUMAN-CURATED (business-only) and is NEVER the table-enumeration source.** Enumeration is **always** the `tables/` directory (§4.3). Wiring enumeration to this 2-entry list would silently drop the 3 infra tables from both the resource set and the function-wiring keys — the "5-table trap." This body is for human progressive-disclosure only.

`tables/pay_runs.md` (we **emit** the SPEC §4.2 **pipe** table; the parser tolerates ga4 bullets best-effort). The pipe form puts Type in its own cell, so `decimal(6,2)` extracts verbatim with zero nested-paren ambiguity:

```markdown
---
type: Unity Catalog Table
resource: serverless_stable_qh44kx_catalog.payroll_demo.pay_runs
title: pay_runs
description: One row per employee per pay period.
timestamp: '2026-06-16T00:00:00+00:00'
tags: [payroll, pay_runs]
---
# Overview            <- Phase-2 enrichment (semantics); IGNORED by Phase-1 loader
# Schema              <- LOAD-BEARING in Phase 1 (drives col(type) list)
| Column | Type | Description |
| --- | --- | --- |
| `run_id` | string | Pay-run identifier. |
| `period_end` | date | Last day of the pay period. |
| `employee_id` | string | FK -> [`employees`](/tables/employees.md). |
| `gross_pay` | decimal(6,2) | Gross pay before deductions. |
| `overtime_hours` | int | Overtime hours in the period. |
| `deductions` | decimal(6,2) |  |
| `net_pay` | decimal(6,2) |  |
# Joins               <- Phase-2 enrichment; FK predicate prose + bundle-relative link
# Examples            <- Phase-2 enrichment; fenced ```sql blocks
```

The `# Schema` pipe table is the **only** load-bearing body section in Phase 1. `# Overview`/`# Joins`/`# Examples` + the Description column are Phase-2 enrichment slots, **ignored** by the Phase-1 loader. The 3 infra tables get frontmatter + `# Schema` only (no enrichment headings).

> **F2 note:** the FK markdown-link lives in the **Description cell (col 3)**, never col 1. The parser reads ONLY the col-1 backtick token for the name and ONLY col-2 for the type — an FK-decorated row still yields exactly `employee_id(string)`.

### 4.2 The `load_baked_schema` seam (totalised — F1/F5)

`load_baked_schema()`'s **return contract stays literally `{catalog, schema, tables}`** — no extra keys. That is why all three callers (incl. the dev-UI landing card at `_wiring.py:431`) are untouched in Phase 1 and need no re-verification. Enrichment rides a **separate** accessor (§6), so we never have to prove the landing card tolerates an extra dict key.

New control flow inside `load_baked_schema` (read-only, **None-on-error**, runs in the deployed App container on every build):

```python
for d in [here, *here.parents]:
    okf_root = d / ".apx" / "okf"
    if okf_root.is_dir():
        try:                                     # F1: totality — mirror the existing json.loads guard
            parsed = _load_okf_schema(okf_root)  # returns {catalog,schema,tables} | None; NEVER raises
        except Exception:
            logger.warning("OKF bundle parse failed at %s; falling back to schema.json", okf_root)
            parsed = None
        if parsed is not None:
            return parsed                        # OKF is source of truth — wins
        # OKF parse-miss -> fall through to schema.json at SAME level (dual-read)
    candidate = d / ".apx" / "schema.json"       # UNCHANGED back-compat branch
    if candidate.is_file():
        try:
            data = json.loads(candidate.read_text())
        except Exception:
            return None
        return data if isinstance(data, dict) else None
return None
```

Rules, pinned:

- **Per-level:** OKF preferred; on OKF parse-miss, fall back to `schema.json` at the **same** level (a half-migrated project still grounds).
- **F1 (load-bearing):** the OKF call is wrapped in `try/except Exception` AND `_load_okf_schema` is internally total (every `OKFDocument.parse` / `_parse_schema_table` / frontmatter read guarded, `fm.get(...)` not `fm[...]`). A malformed bundle (bad YAML, mangled pipe row, missing `datasets/*.md`) yields `None` and falls through — it **never** raises out of `load_baked_schema`. This converts the boot-crash window back into graceful ungrounding and satisfies OKF §9.
- **Read-only:** the loader NEVER writes. The App FS may be read-only; cache regen happens only at scaffold/refresh/migrate/build. No network, no in-loader cache rewrite.

`_load_okf_schema(okf_root: Path) -> dict | None` (in `_schema.py`):

1. **Dataset concept (F10):** glob `okf_root/datasets/*.md` (skip `index.md`); if more than one, take the first by `sorted()` (deterministic precedence). `OKFDocument.parse`; read `catalog = fm.get("catalog")`, `schema = fm.get("schema")`. If **either** is missing → return `None` (degrade). If a value is present but `!= ` the constructor catalog/schema, the `data_agent.py:80–84` gate already drops to ungrounded; the loader additionally `logger.warning`s the mismatch so a bad human edit is visible, not silent.
2. **Table ordering (F3):** **primary** = `sorted(p for p in (okf_root/"tables").glob("*.md") if p.name not in {"index.md", "log.md"})`. **Advisory override:** if `tables/index.md` lists `[title](path.md)` links, use that order. (OKF's `regenerate_indexes` treats `index.md` as a derived artifact it may rewrite/reorder, so we never bind load-bearing order to it alone.) For payroll, `sorted()` == the current `schema.json` insertion order (alphabetical) — byte-for-byte, no `index.md` needed.
3. **Per table in order:** `OKFDocument.parse`, name = `title` (or filename stem), `cols = _parse_schema_table(body)`. A missing/empty `# Schema` yields `[]` (never raises) and the table is **still emitted** (name is load-bearing).
4. **Return** `{"catalog": catalog, "schema": schema, "tables": {name: [col(type), ...]}}` — byte-identical shape to today's `schema.json` dict.

`_load_okf_schema` is the single converter `okf_manifest(okf_root)`, called by **both** the loader and the cache-writer, so the cache equals what the loader returns.

### 4.3 The derived `schema.json` cache

`.apx/schema.json` is demoted to a **committed, regenerated derived cache**, never hand-edited. It is regenerated from the bundle at scaffold/refresh/migrate/build. When **both** `.apx/okf/` and `.apx/schema.json` exist, **OKF wins** (read first in the per-level loop), so a stale cache can never mis-ground a migrated project — the cache is read only when no `.apx/okf/` is present at that level. Documented as generated in README + `migrate-to-okf` output. (Why keep it committed: dual-read fallback, zero-parse fast path, and external readers keep working — see §7.)

---

## 5. Converter mapping (`schema.json` ⇄ OKF)

Column-string format = `"<col>(<type>)"`. Verified payroll examples: `employee_id(string)`, `gross_pay(decimal(6,2))`, `hire_date(date)`, `tags(array<string>)`, `embedding(array<float>)`, `created_at(double)`, `overtime_hours(int)`.

### Field-by-field map

| `schema.json` | OKF |
|---|---|
| `catalog` | `datasets/<schema>.md` frontmatter `catalog:` |
| `schema` | `datasets/<schema>.md` frontmatter `schema:` |
| `tables{}` keys | `tables/<name>.md` file stem / `title:` |
| `"col(type)"` | one `# Schema` **pipe row** `\| \`<col>\` \| <type> \| <desc> \|` (col-1 backtick token, col-2 type cell) |
| (col description) | `# Schema` pipe col-3 — **new** info, no `schema.json` equivalent (Phase 2) |
| (table semantics) | `# Overview` prose — **new** (Phase 2) |
| (join hints) | `# Joins` prose + FK md-links — **new** (Phase 2) |
| (canned SQL) | `# Examples` ` ```sql ` fences — **new** (Phase 2) |

### `_parse_schema_table(body) -> list[str]` — the reverse (OKF → col-string), totalised

The piece the reference lacks. Parses the `# Schema` section; handles **both** the SPEC pipe table and the ga4 bullet form. **Guarantees:**

- **F2 — drop non-data rows:** emit a column only when col-1 contains a **backticked identifier** matching `` r"`([A-Za-z_][A-Za-z0-9_.]*)`" `` (mirrors the reference regex). This skips the un-backticked `| Column | Type | Description |` header and the `| --- | --- | --- |` separator. A full pipe table incl. header+separator yields exactly *N* column entries, no `Column`/`---` artifacts.
- **Pipe form:** col-1 backtick token = name (strip backticks + any FK markdown-link wrapper `[\`col\`](...) -> col`, defensive for bullet forms only — never runs on col-3); col-2 Type cell taken **verbatim** → so `decimal(6,2)` and `array<string>` are clean (the `,` and `<>` are isolated in their own cell, never colliding with `|` or parens). Emits `f"{col}({type})"`.
- **ga4 bullet form** (`` - `event_date` (STRING): desc ``) — **best-effort lossy** (F6): emits `event_date(STRING)`. **Not** claimed robust to the real ga4 `##`/`###` RECORD hierarchy with 4-space-indented dot-path bullets; nested fields may be dropped. apx **emits** pipe tables for its own bundles, so lossy ga4 extraction is acceptable; the claim is scoped to match.
- **F6 — totality on absence:** if the `# Schema` heading is **absent** (real datasets/metrics/joins have none), return `[]` — never `None`, never a raise. `schema.json` already permits empty column lists, so `[]` is the correct degrade and keeps the load-bearing table **name**.

### Forward (col-string → OKF; scaffold + `migrate-to-okf`)

- For each `"<col>(<type>)"`: split on the **first** `(` for the col name; type = everything between that first `(` and the **last** `)`. Yields (`gross_pay`, `decimal(6,2)`) and (`tags`, `array<string>`) correctly — **not** first-`)`. Column names never contain `(`, so this is robust.
- Emit `| \`<col>\` | <type> | <comment-or-blank> |`. Description seeded from the UC column `.comment` when `introspect_schema_columns` is extended to capture it (else blank, awaiting Phase-2 enrichment).
- **F9 — escape `|`:** on emit, escape any `|` in a column comment as `\|` (or strip newlines/pipes). Phase-1-safe today only because descriptions are blank; once `.comment` is captured, an unescaped `|` shifts the Type cell and silently corrupts the type.
- **Type fidelity:** keep UC `type_text` **verbatim** (lowercase `decimal(6,2)`, `date`, `array<string>`). Do NOT remap to ga4 uppercase BigQuery legacy names — lossy and wrong for a UC asset.
- `catalog`/`schema` (top-level) → `datasets/<schema>.md` frontmatter `catalog:`/`schema:`.
- Emit `tables/index.md` to pin order (so reverse load is order-stable for projects whose introspect order is non-alphabetical; `ws.tables.list()` order is not guaranteed alphabetical in general).
- Emit all 4 `REQUIRED_FRONTMATTER_KEYS` per concept (ISO-8601 `timestamp`) or the bundle fails the reference validator.

### Discriminating round-trip test (must pass both directions)

`gross_pay(decimal(6,2))` and `tags(array<string>)` → pipe row → back. Column order preserved (pipe order = ordinal from introspect). **Verified safe:** the pipe table isolates the type in its own cell, so the nested-paren/angle mangling does not occur in the stated path.

---

## 6. Auto-gen at scaffold + enrichment reaching the prompt

Phasing reconciles the locked "`build_instructions` unchanged + zero `data_agent` change" constraint with enrichment (which inherently touches the prompt). They coexist only by phasing.

### Phase 1 — transparent substrate swap (independently shippable)

`load_baked_schema` returns the byte-identical `{catalog, schema, tables}` dict from OKF. Auto-generated bodies (pipe `# Schema` tables, blank descriptions) ground **exactly** as `schema.json` does today. `data_agent.py`, `build_instructions_from_schema`, resources, wiring: **literally untouched**.

### Enrichment authoring (works in both phases)

The bundle is diffable + version-controlled (`.apx/` is **tracked**, not gitignored — confirmed: only `.apx.local` and `.apx-builder.json` are ignored). Humans/agents edit the markdown **bodies**: fill the Description column in `# Schema` rows, add `# Overview` semantics, add `# Joins` predicate prose + bundle-relative FK links (e.g. `pay_runs.md` links `employee_id` → `/tables/employees.md`), add `# Examples` ` ```sql ` blocks. Git-diffable, PR-reviewable; never touches frontmatter `catalog`/`schema`. The existing Edit-page NL tool-gen can target these bodies. Scaffold/migrate emit `# Overview`/`# Joins`/`# Examples` as **stub headings** for humans/agents to fill.

### Phase 2 — additive enrichment reaching the prompt (gated, separate from Phase 1)

**Delivery decision — a new `grounding=` param on `build_instructions_from_schema`; reject the `instructions=`-override delivery.** Decisive rationale: the build path does `resolved_instructions = instructions or build_instructions_from_schema(...)` (`data_agent.py:132`). Delivering enrichment through `instructions=` would short-circuit and **silently drop both the enrichment AND the schema block** whenever a user sets `[tool.apx.agent].instructions`, and would force wiring into the user-editable scaffolded `agent.py`. The `grounding=` param has neither problem: a user-supplied `instructions=` still wins (consistent with today), and the weaving stays inside the framework.

Mechanism (verified seam — enrichment is isolated to the instruction path: `tables` **values** are consumed ONLY by `build_instructions_from_schema`/`_format_schema_block`; `data_agent.py:117` resources + `:124` `uc_function_toolkit` use `tables.keys()` only):

- `load_okf_grounding(start=None) -> dict | None` — a **separate** accessor in `_schema.py`, same upward walk, returns the optional per-table enrichment payload `{ "<table>": {"description", "columns":[{name,type,description}], "joins": "<# Joins prose>", "examples": "<# Examples sql>"}, ... }` harvested from the OKF bodies, or **`None`** when no body carries enrichment beyond auto-gen (so un-enriched bundles render byte-identical even in Phase 2). Same totality discipline as `_load_okf_schema` (None-on-error, never raises). Only the build path calls it; the landing card and refresh never do.
- `build_instructions_from_schema(catalog, schema, tables, persona=None, objective=None, grounding: dict | None = None)`: when `grounding is None` (legacy `schema.json` path, un-enriched bundle, `tables=` override, or live introspect) → render **exactly** today's output via `_format_schema_block` — **default-identical**, every existing project and test stays green. When `grounding` is present → `_format_grounded_schema_block` weaves per-table description + first-N columns-with-descriptions + a one-line distilled `# Joins` + at most one truncated `# Examples` SQL; the "you already know the schema / do NOT run SHOW TABLES" framing + chain + safety paragraphs are unchanged.
- The **only** downstream deviation: a single one-line pass-through in `_build_data_tools_and_instructions` (`data_agent.py:132`) forwarding `grounding=load_okf_grounding() if baked-was-the-source else None`. This is the single permitted Phase-2 deviation from "zero `data_agent` change," flagged explicitly. (`tables=` override and live introspect supply no grounding → identical-to-today output.)

> **F7 — keep ALL tables in the grounded block (resolved).** `_format_grounded_schema_block` renders **every** table (infra included), so for tables without enrichment the grounded path stays **prompt-identical to Phase 1**. The earlier proposal to *exclude* the 3 infra tables is **not shipped** — it would silently drop 3 tables from the payroll prompt the moment any enrichment landed (a prompt change gated on enrichment-presence, not an opt-in flag, and not covered by the Phase-1 equivalence gate). Infra tables remain both `uc_table` resources AND prompt-block lines; enrichment is purely additive on top of the existing per-table line.

### Token bounding (designed in)

`_format_grounded_schema_block` **mirrors** `_format_schema_block`'s caps (max 12 cols/table with `(+N more)`, max 20 tables) and adds: per-table description ~1 sentence, joins distilled to one line, at most one example SQL truncated to ~6 lines. Measure the token delta on the 5-table payroll bundle in the spike.

### Build-time reach

The bundle ships via `cp -r .apx .build/` (`databricks.yml:45`) and the parser ships in the wheel, so `load_okf_grounding` runs in the deployed App container at agent construction (same place `load_baked_schema` runs today) — enrichment reaches the served prompt with **no** deploy-pipeline change beyond the `cli.py:1113` template fix (F8).

---

## 7. Migration & back-compat

payroll-coworker ships `.apx/schema.json` **today** with five tables; each is a `uc_table` ResourceSpec (`data_agent.py:117`), so the table **set** is load-bearing, not cosmetic.

1. **Dual-read back-compat (permanent, not transitional).** `load_baked_schema` prefers `.apx/okf/` if present, else falls back to the untouched `.apx/schema.json` `json.loads` path. Un-migrated projects in the wild keep working **byte-for-byte**. Zero forced migration, zero behavior change.

2. **New command `apx-agent schema migrate-to-okf` (auto-migrate is OPT-IN/explicit).** Reads existing `.apx/schema.json` via `load_baked_schema`, runs the **forward** converter to emit `.apx/okf/`, then regenerates `.apx/schema.json` as the derived cache. **Captures all 5 tables** — the 3 infra/memory tables (`agent_memory`, `apx_payroll_coworker_memory`, `apx_payroll_coworker_sessions`) become **minimal concepts** (frontmatter + `# Schema` pipe table only). This guarantees the regenerated cache is byte-identical and the `uc_table` resource set is unchanged. Idempotent; refuses to clobber an enriched bundle without `--force`. (Filtering infra tables OUT of grounding is a **separate** product decision, explicitly **not** part of this swap.)

3. **Scaffold emits OKF for NEW projects** (`cli.py:1879` + `cli.py:2038` switch to writing bundle + derived cache).

4. **Cache = keep-and-regenerate, committed.** Reject gitignoring it — it's the dual-read fallback + zero-parse fast path and lets external readers keep working. When both exist, OKF wins, so a stale cache can never mis-ground a migrated project. Documented as generated in README + `migrate-to-okf` output. Mitigate drift (human edits `okf/`, forgets to regen) with a refresh/pre-commit regen hook; a stale cache can't mis-ground (OKF wins) but external readers go stale.

5. **Deploy-copy fix (F8 — highest-severity correctness item).**
   - The scaffold-template heredoc at `cli.py:1113` copies `.apx-agent` but has **no** `cp -r .apx .build/` line. New projects from this template would NOT ship the OKF bundle (or even today's `schema.json`); `load_baked_schema` walks up from the deployed `./.build` cwd and silently returns `None` → **ungrounded in prod** (passes locally, fails deployed).
   - payroll-coworker's already-deployed `databricks.yml:45` **has** `cp -r .apx .build/ 2>/dev/null || true` (verified — it was patched). So payroll already ships `.apx/` correctly; only the generator template is stale. (Verified there is exactly one build heredoc in `cli.py`, ~1103–1117, so a single add covers all archetypes.)
   - **Required spike step:** add `cp -r .apx .build/ 2>/dev/null || true` to the `cli.py:1113` heredoc + a `caps`/`ctk` read-after-deploy check that `./.build/.apx/okf/` is present in the deployed container.

**Memory/session tables handling:** the 3 infra tables (`agent_memory`, `apx_payroll_coworker_memory`, `apx_payroll_coworker_sessions`) are migrated as minimal concepts and remain in both the resource set and (Phase 1 & 2) the prompt block. They are NOT dropped here.

---

## 8. Open-format / no-lock-in GTM for Databricks

### 8.1 Positioning (one paragraph)

apx turns a customer's Unity Catalog into a pre-grounded coworker — and with the Open Knowledge Format (OKF), that grounding becomes a portable, open-format bundle the customer *owns*, not a proprietary artifact locked inside apx. The bundle is the single source of truth for what the agent knows about their data; apx auto-generates it from UC metadata and runs it under UC governance. The open format removes the lock-in objection. The moat is not the format — it's that apx is the only thing that generates this bundle **natively from Unity Catalog** and executes it **under UC grants and end-user identity passthrough**. Open grounding earns trust; UC-native, governed grounding is why apx wins.

### 8.2 The grounding story: before / after

**Before.** Grounding lived in a proprietary `.apx/schema.json` manifest. It worked, but it was an apx-shaped artifact: the customer's hard-won semantic knowledge (table meanings, example queries, join paths) was trapped in our format. Reasonable buyer question: *"If I invest in grounding my coworker, am I locked into apx?"* We had no clean answer.

**After.** Grounding lives in an open OKF bundle the customer owns and version-controls. `.apx/schema.json` becomes a **derived cache generated from OKF** — an implementation detail, no longer the source of truth. The customer's semantics are in a diffable, vendor-neutral, Apache-2.0 format that travels with them. Mechanically this is low-risk: the OKF parse flows through the existing `load_baked_schema()` seam, so every downstream caller (`uc_table` resources, function wiring, instruction building) is unchanged. The buyer question now answers itself: *your grounding is yours, in an open format; apx is the best engine to generate and govern it.*

### 8.3 Why this is strong for Databricks specifically

- **It rhymes with Databricks' own open-ecosystem strategy.** Delta Lake, Unity Catalog OSS, and MLflow were all open-sourced. apx adopting an open grounding substrate is on-brand for the platform, not a concession — "open format, governed execution" is the same playbook Databricks already runs.
- **Unity Catalog is the metadata source.** The bundle is auto-generated from UC (Tables API / UC metadata), so grounding stays anchored to the catalog of record. UC is upstream; the open bundle is downstream. This deepens UC's gravity rather than routing around it.
- **Governance is the differentiator.** Anyone can parse an open OKF file. Only apx executes it under **UC grants and end-user identity passthrough (OBO)** — the agent sees exactly what the asking user is permitted to see. Open format + governed execution is a combination a generic OKF reader cannot match.

### 8.4 The sales motion

1. **Connect.** Point apx at a Unity Catalog schema.
2. **Auto-ground.** Scaffold generates the OKF bundle from UC metadata — same one-command experience as `schema.json` today, now emitting an open, owned artifact.
3. **Enrich.** Humans or agents fill in the markdown bodies — semantics, example queries, join paths — the knowledge UC metadata alone can't capture. Diffable, reviewable, version-controlled.
4. **Govern + serve.** The coworker runs grounded, under UC grants and identity passthrough.
5. **Land the ownership message.** "Your grounding is an open bundle you own and can take with you. apx is the engine that generates it from UC and runs it under your governance." Open removes the lock-in fear; UC-native + governed is why they stay.

**Demo beat:** `git diff` on `.apx/okf/tables/pay_runs.md` showing an SA enriching join semantics → the agent's prompt improves with no code change. Differentiator vs black-box RAG: no lock-in (any OKF consumer can read the bundle), governed, PR-reviewable.

### 8.5 Honest risks and caveats

- **The no-lock-in claim only holds if our bundles are genuinely spec-compliant.** This is a commitment, not a freebie: if apx emits OKF-flavored-but-nonconformant bundles, the portability pitch is hollow and we've made a worse lock-in (a fake-open one). Spec-conformance must be a tested, enforced property of every bundle we generate — treat it as a release gate, not an aspiration (see §10).
- **OKF is a v0.1 draft originating in a GoogleCloudPlatform repo.** We are betting our grounding substrate on someone else's early-stage, vendor-originated format. Real exposures: spec churn (breaking changes between drafts), governance/direction risk (we don't steer the format), and the optics of adopting a GCP-originated standard inside a Databricks motion. Mitigation posture: frame apx as *adopting* an open standard (table stakes), keep the derived cache as an insulation layer so spec changes hit one parser not the whole stack, and track the spec's maturity before we make portability a contractual promise.
- **Don't overclaim authorship or maturity.** We did not write OKF and it is not 1.0. The defensible story is narrow and true: UC-native auto-generation + governed grounding. Keep the pitch there.

---

## 9. Spike plan (the SEPARATE, gated next step)

> **Gate:** this spike runs **only after the user approves this spec.** It must **execute** and be **inspected** — a passing self-test that doesn't actually run the loader proves nothing.

Target: `python/payroll-coworker/` (worktree-isolated — concurrent repo activity has deleted `python/` before; do spike work in a `git worktree`).

1. **Vendor `_okf.py`** — `OKFDocument.parse/serialize/validate` (exact mirror) + `_parse_schema_table` (pipe + best-effort bullet, F2/F6 guarantees).
2. **Implement `migrate-to-okf`** — convert payroll's `.apx/schema.json` → `.apx/okf/` (all 5 tables; 3 infra minimal), regenerate the cache.
3. **Totalise & branch `load_baked_schema`** — add the guarded OKF branch (F1).
4. **Point DataAgent at it** — build the payroll agent with the OKF bundle present.
5. **Diff generated instructions + `uc_table` resources vs today** (grounding=None / Phase 1):
   - **(a) dict-equality** — `load_baked_schema()` over `.apx/okf/` == the current `.apx/schema.json` dict (order-insensitive; satisfies the `data_agent.py:80–84` gate).
   - **(b) prompt-string identity** — the rendered `build_instructions_from_schema` output is **byte-identical** (order-sensitive; `_format_schema_block` iterates keys in order). For payroll, `sorted()` == insertion order, so both hold without needing `index.md`.
   - **(c) resource identity** — the same five `uc_table` ResourceSpecs.
6. **Deploy-copy fix (F8)** — patch the `cli.py:1113` heredoc; add the `caps` read-after-deploy check that `./.build/.apx/okf/` is present in the deployed container.
7. **Phase-2 smoke (optional within spike):** enrich `pay_runs.md` (`# Joins` + one `# Examples`), confirm `grounding=`-routed block stays within caps and measure the token delta.

**Exit criteria:** (a)+(b)+(c) green on the live payroll bundle; malformed-bundle test falls through to cache / `None` without raising; deployed `./.build/.apx/okf/` present.

---

## 10. Testing & conformance

**Equivalence gate (Phase-1 transparency proof) — BOTH required:**
- (a) **dict-equality** (order-insensitive) — the gate at `data_agent.py:80–84` and the "loader == schema.json" test.
- (b) **prompt-string identity** (order-sensitive) — `_format_schema_block` iterates keys in insertion order. migrate/refresh MUST keep `tables/index.md` in sync with the table files (or rely on `sorted()` where it matches) or (b) silently diverges while (a) still passes.

**Round-trip:** `okf_manifest(emit(manifest)) == manifest`, with `gross_pay(decimal(6,2))` + `tags(array<string>)` both directions, plus a **deliberately non-alphabetical** table set (proves `tables/index.md` pins order for projects where introspect order ≠ alphabetical).

**Totality / no-raise (F1):** a malformed `okf/` bundle (bad YAML; mangled pipe row; missing `datasets/*.md`) → `load_baked_schema` falls through to the `schema.json` cache, and if that is absent → returns `None`. **Never raises.**

**Parser-hardening tests:**
- F2 — a full pipe table incl. header+separator → exactly *N* column entries, no `Column`/`---` artifacts; prompt-string byte-identical to the `schema.json` render.
- F3 — a bundle with empty `tables/index.md` still yields exactly the 5 real table keys, **no `index` key**, identical `uc_table` ResourceSpec set.
- F6 — a concept with **no** `# Schema` → `_parse_schema_table` returns `[]`, table name preserved.
- FK row — `| \`employee_id\` | string | FK -> [\`employees\`](/tables/employees.md) |` → exactly `employee_id(string)` (FK-link-strip never runs on col-3).
- F9 — a comment containing `|` round-trips without corrupting the Type cell (escape on emit).

**Conformance (release gate — the GTM commitment, §8.5):** every non-reserved `.md` emitted by apx has parseable YAML frontmatter with a non-empty `type`; all 4 `REQUIRED_FRONTMATTER_KEYS` present; `OKFDocument.validate()` passes on **emit** (producer-side only). Lean on §9 permissive-consumption (tolerate unknown keys/types/broken links/missing `index.md`) on the read side.

**Downstream identity:** same `uc_table` ResourceSpecs and same `build_instructions_from_schema` output before vs after the swap (grounding=None).

---

## 11. Risks, open questions, YAGNI cuts

### Risks / footguns (all with their resolution)

- **Loader totality (F1) — RESOLVED.** Guarded OKF branch + internally-total `_load_okf_schema`; `fm.get(...)` not subscript; None-on-error. This is the one regression that would have made the new path *less* safe than the code it replaces (boot-crash vs graceful ungrounding); it is closed.
- **Deploy-copy divergence (F8) — RESOLVED (template fix + caps check).** Highest-severity correctness item; the generator template lacks `cp -r .apx` though payroll's deployed bundle is already patched.
- **catalog/schema source + byte-match.** Read explicit `catalog:`/`schema:` frontmatter keys (never parse the `resource:` URI). On mismatch the `data_agent.py:80–84` gate already drops to ungrounded; the loader additionally logs a warning.
- **Phantom-column / phantom-table (F2/F3) — RESOLVED.** Backtick-required col-1; reserved-filename exclusion; `sorted()` primary.
- **Enumeration-source (F4/5-table trap) — RESOLVED.** Enumerate from the `tables/` directory, never the dataset `# Tables` body.
- **Nested-paren types.** Forward split on first-`(`/last-`)`; reverse is a clean concat from the isolated pipe Type cell. Dedicated test.
- **Phase-2 "default-identical" (F7) — RESOLVED.** Keep all tables in the grounded block; infra exclusion not shipped.
- **Prompt bloat (Phase 2).** Mitigated by `_format_grounded_schema_block` caps mirroring the 12-col/20-table limits; measure token delta on payroll.
- **Pipe vs bullet (F6).** apx **emits** pipe (owned, round-trippable, clean type cell); parser **tolerates** ga4 bullet best-effort (lossy, claim scoped honestly).
- **Vendor drift.** Pin `_okf.py` to SPEC §4 + a citing comment; re-check on `okf_version` bumps. Lean on §9 permissive-consumption.
- **Stale cache.** Can't mis-ground (OKF wins) but external readers go stale; mitigate with a refresh/pre-commit regen hook; document cache as generated.
- **`validate()` on the read path (F5) — PINNED OUT.** Emit-side only; read path uses `.parse`.

### Open questions

- **Q1.** Should `refresh-schema` re-introspect **only** the `# Schema` section per table (preserving human-enriched `# Overview`/`# Joins`/`# Examples` via `OKFDocument` round-trip — the reference's augmentation-guard pattern)? Proposed **yes**; confirm in spike.
- **Q2.** Pre-commit hook vs build-step for cache regen — which is the canonical regen point? (Both proposed; build-step is mandatory, pre-commit is convenience.)
- **Q3.** When `[tool.apx.agent.data].instructions` is set by the user AND enrichment exists — today `instructions=` wins and enrichment is dropped (consistent with the locked decision). Is a future "merge" mode wanted, or is "user instructions win, full stop" the permanent contract? (Currently the latter.)

### YAGNI cuts (explicitly NOT in this design)

- **Filtering infra tables out of grounding** — a separate product decision; not part of this swap. All 5 tables carry over.
- **Depending on `enrichment-agent`** — rejected (not on PyPI; drags google-adk/bigquery/markdownify). Vendor a ~40-line `_okf.py` + the schema-table parser the reference lacks.
- **Adding extra keys to `load_baked_schema`'s return** — rejected; enrichment rides the separate `load_okf_grounding()` accessor so the landing card / `_wiring.py:431` need no re-verification.
- **Routing enrichment through `instructions=`** — rejected (short-circuits `resolved_instructions = instructions or ...`).
- **Emitting ga4-style bullet `# Schema`** — not a v1 requirement; we emit pipe, tolerate bullet on read.
- **In-loader cache rewrite** — rejected; the App FS may be read-only and the loader must stay read-only/None-on-error.
- **`okf_version` as required** — optional per §11; we emit it in the root `index.md`, but the loader never requires it.
