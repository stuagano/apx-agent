# PRD: Handoff-Ready Proof Tier — prove a build is safe to hand over

**Version**: 1.0 | **Status**: Draft | **Date**: 2026-09-11 | **Slug**: `handoff_ready` | **Issue**: #760

## Summary
Add a **`handoff`** capability tier to caps that proves a build can be **run and
owned by someone other than its author** — the same claim-vs-reality read-back
discipline caps already applies to runtime promises, pointed at the handoff.
The tier splits into **cheap static checks** (parse the bundle / `databricks.yml`,
run in CI with no workspace) and **live read-back checks** (query a target
profile). It reuses the existing tier plumbing verbatim — `caps verify/gate/ack/
status`, the `ledger.py` evidence entry, the `runner.py` pytest/shell
classification, the `python/checks/_live.py` prove-script contract — so there is
**no new command and no new report format**. A new pure-Python lint module
(`python/src/apx_agent/_handoff_lint.py`) is the one piece of new logic, and it is
shared by the cheap pytest gate and the live prove script (Ponytail reuse).
**Primary success metric: failing handoff-check count → 0**
(`caps verify --tier handoff` cheap checks green in CI +
`cd python && uv run pytest -k handoff` green + `make check` green).

## Background
**What exists today (all reusable):**
- **Tier enum + freshness defaults.** `caps/manifest.py:13`
  `VALID_TIERS = ("cheap", "live")` and `caps/manifest.py:14`
  `DEFAULT_FRESHNESS = {"cheap": "code", "live": "24h"}`. A `Capability`
  (`caps/manifest.py:18`) carries `tier`, `freshness`, `check_kind`
  (`"pytest" | "shell"`), and `check_target`. `_parse_check`
  (`caps/manifest.py:33`) turns a bare string `check:` into a **pytest** node and
  a `{shell: ...}` mapping into a **shell** command — the exact seam a handoff
  live check uses (`check: {shell: "python checks/prove_handoff_ready.py ..."}`).
- **CLI tier surface.** `caps/cli.py` declares `--tier` choices in **three** spots:
  status (`caps/cli.py:452`), verify (`caps/cli.py:459`), and add
  (`caps/cli.py:528`), each `choices=["cheap", "live"]`. `verify --stale`
  (`caps/cli.py:229`) re-proves only `c.tier == "cheap"` when no `--tier` is
  passed — so handoff-static is **not** auto-gated by an empty `verify`; CI runs
  it explicitly via `caps verify --tier handoff` (a known ceiling, noted below).
- **Runner.** `caps/runner.py` runs a `pytest` check (`caps/runner.py:123`,
  exit 0→pass, 1→fail, other→error) or a `shell` check, and records the tail of
  failed output. A handoff static check is `check_kind="pytest"`; a handoff live
  check is `check_kind="shell"` invoking the prove script.
- **Ledger evidence (automatic, per-capability).** `caps/ledger.py:12`
  `LedgerEntry(result, at, tier, ..., detail, duration)` where `result` is
  `"pass" | "fail" | "error" | "waived"`. Every handoff check records the same
  shape as cheap/live with **no ledger change** — the ledger already stores `tier`
  so `handoff` entries are first-class.
- **Live prove-script contract.** `python/checks/_live.py` provides
  `require(*names)` (env or exit-3 `cannot_run`), `make_ws(profile)`,
  `proven(msg)` (exit 0), `disproven(msg)` (exit 1), `cannot_run(msg)` (exit 3),
  and `guard(fn)`. Exit 3 → caps records `error`/waived in CI without a profile —
  the same boundary the live tier already lives behind.
- **Opt-in-mutate precedent.** `python/checks/prove_canary_split.py` gates any
  workspace mutation behind `require("APX_CAPS_CANARY_ALLOW_MUTATE")` — the
  handoff live "clean deploy" check reuses this exact opt-in flag so a bare
  `caps verify --tier handoff` never deploys.
- **PRD/AC + reality precedent.** `prd_tool_scoped_auth.md` (#758) is the template;
  its **AC-8** is the live-gated `skip_reason: requires APX_CAPS_PROFILE` pattern
  this PRD mirrors for every live read-back.

**What's missing.** There is no `handoff` tier, no bundle/config linter, and no
prove script that reads a build's ownership/idle/deploy facts back. Notably there
is **no generic `databricks.yml` parser in `src/apx_agent`**: `_project_gen`
*writes* `databricks.yml` as a string, `_canary_apps`/`_hot_swap_apps` read only a
few vars out of `cwd/databricks.yml`, and `_doctor` does existence-only checks.
So the static checks parse `databricks.yml` directly with **PyYAML** in a new
pure-function module reused by both the pytest gate and the live prove script.

## Research Inputs
- `caps/manifest.py` — `VALID_TIERS` (:13), `DEFAULT_FRESHNESS` (:14),
  `Capability` (:18, `check_kind`/`freshness`), `_parse_check` (:33,
  string→pytest / `{shell:…}`→shell), `load_manifest` (:44).
- `caps/cli.py` — `--tier` choices at :452 (status), :459 (verify), :528 (add);
  `verify --stale` cheap-only re-prove at :229.
- `caps/runner.py` — pytest/shell run + exit-code classification (:118–:150).
- `caps/ledger.py:12` — `LedgerEntry(result, at, tier, detail, duration)`
  (per-capability evidence; `tier` already stored).
- `python/checks/_live.py` — `require`/`make_ws`/`proven`/`disproven`/
  `cannot_run`/`guard` (exit 0/1/3 contract).
- `python/checks/prove_canary_split.py` — `APX_CAPS_CANARY_ALLOW_MUTATE` opt-in
  mutate; `prove_uc_publish.py` — `require()` + read-back idiom.
- `python/capabilities.yaml` — entry schema (`id/description/given/when/then/
  tier/deps/check`, cheap pytest string vs live `{shell:…}`).
- `python/src/apx_agent/_project_gen.py` (`_build_databricks_yml`) — bundle shape;
  `_canary_apps.py`/`_hot_swap_apps.py` — existing `databricks.yml` reads.
- `prd_tool_scoped_auth.md` — PRD template + AC-8 live-gated `skip_reason`.

## Goals
- A **`handoff` tier** exists and runs through the existing
  `caps verify/gate/ack/status` and records ledger evidence — no new command.
- **Cheap static checks** parse a bundle / `databricks.yml` and gate CI with no
  workspace: timeouts, auto-stop/scale-to-zero, no personal owner, FQN tables,
  paused/owned schedules.
- **Live read-back checks** prove the facts against a target profile: SP
  ownership reads back, a clean `bundle deploy` stands the set up (opt-in mutate),
  auto-stop/scale-to-zero read back live, created-resource inventory lists back.
- **Per-check ledger evidence**, identical shape to cheap/live.
- **Framework-transparent:** clear pass/fail per check, actionable messages, no
  hand-wiring.

## Non-Goals (v1)
- **Auto-remediation** — checks report pass/fail, they do not fix the bundle.
- **Teardown execution** (`bundle destroy`) — checks readiness only.
- **Config-surface export automation** (Genie space export, etc.) — a later check.
- **A separate `caps handoff` command or bespoke report** — rejected; reuse the
  tier model (`--tier handoff`) and the existing ledger/status output.

## Requirements
### Functional
- **FR-1 — Register the `handoff` tier.** Add `"handoff"` to
  `caps/manifest.py:13` `VALID_TIERS`, add `"handoff": "code"` to
  `caps/manifest.py:14` `DEFAULT_FRESHNESS` (static checks default to `code`;
  live checks set `freshness: 24h` per-capability), and add `"handoff"` to the
  three `--tier` `choices` lists in `caps/cli.py` (:452 status, :459 verify,
  :528 add). No change to `runner.py`, `ledger.py`, `gate`, `ack`, or `status`
  logic — they already key off `tier`/`check_kind` generically.
- **FR-2 — Static lint module (shared).** New
  `python/src/apx_agent/_handoff_lint.py`: pure functions, each taking a parsed
  bundle `dict` and returning a **`list[str]` of violation messages** (empty list
  = clean; no `tuple[...]` returns). Functions:
  `check_job_timeouts`, `check_warehouse_autostop_and_scale_to_zero`,
  `check_no_personal_owner`, `check_tables_fqn`, `check_schedules_paused_or_owned`,
  plus `lint_bundle(bundle) -> list[str]` aggregating them and
  `load_bundle(path) -> dict` (PyYAML). Each violation message names the offending
  path/identifier (e.g. `"job 'ingest' task 'load' has no timeout_seconds"`) so
  evidence is real, not a bare count. **This module is imported by both the cheap
  pytest gate and the live prove script** — one linter, two callers.
- **FR-3 — Cheap static checks (CI-safe, pytest).** `capabilities.yaml` gains
  `handoff`-tier entries whose `check:` is a pytest node in
  `python/tests/test_handoff_ready.py`. Each test loads a fixture bundle and
  asserts the matching `_handoff_lint` function: (a) every job/task has
  `timeout_seconds`; (b) every warehouse has an explicit auto-stop and every
  serving endpoint sets `scale_to_zero`; (c) no bundle-declared owner
  (`run_as`, `permissions`, `owner`) is a personal `@databricks.com` identity;
  (d) tables are addressed by fully-qualified `catalog.schema.table` from config,
  not current-catalog (static heuristic: bare single-segment / `${...}`-catalog-
  less references flagged); (e) schedules are `PAUSED` (dev) or carry a named
  owner. Freshness `code`; gated in CI via `caps verify --tier handoff`.
- **FR-4 — Live read-back checks (profile-gated, shell prove script).** New
  `python/checks/prove_handoff_ready.py` using `_live.py`
  `require`/`make_ws`/`proven`/`disproven`/`cannot_run`/`guard`. It
  `require("APX_CAPS_PROFILE")` and reads back real facts via the Databricks
  SDK/CLI: **(a)** each job/pipeline `run_as`/owner reads back as a **service
  principal**, not a user; **(b)** *behind* `require("APX_CAPS_CANARY_ALLOW_MUTATE")*
  a clean `bundle deploy` to a fresh target stands the set up (deploy-and-read-
  back, **not** `validate`) and the deployed resources read back; **(c)**
  warehouse auto-stop / endpoint `scale_to_zero` read back from the **live** def
  (not the file); **(d)** the created-resource inventory lists back. Without a
  profile the script exits 3 (`cannot_run`) → caps records `error`/waived, same
  as the live tier. Each fact is a separate `handoff`-tier `{shell:…}` capability
  (or sub-check) so ledger evidence is per-fact.
- **FR-5 — Per-check ledger evidence.** No new code: each handoff capability's
  `caps verify` run writes a `LedgerEntry(result, at, tier="handoff", detail,
  duration)` exactly like cheap/live.

### Non-functional
- **NFR-1 — Reuse, no new command.** Everything routes through
  `caps verify/gate/ack/status --tier handoff`; the only new logic is the linter +
  prove script.
- **NFR-2 — Ctk (read-back, not existence).** Live checks assert a real
  read-back fact (SP owner string, live auto-stop value), never a bare exit-0 or
  `.exists()`. The reality AC (AC-8) proves the SP-owner fact via
  `ctk.verify(Artifact(...))`.
- **NFR-3 — No lint-ban regressions.** No `.get(k, "")`, no `x or ""`, no invented
  env defaults, no `object` annotations, no `tuple[...]` returns, no skipped tests.
- **NFR-4 — Untracked-file safe.** `python/capabilities.yaml` and
  `python/checks/` are locally git-excluded; the tracked, shipped-test-only
  change is `caps/` (tier registration) + `python/src/apx_agent/_handoff_lint.py`
  (tracked, importable) + `python/tests/test_handoff_ready*.py`.

## Design
### Flow
```
databricks.yml ──PyYAML──> _handoff_lint.load_bundle(path) -> dict
        │
        ├── cheap (CI, no workspace):  pytest test_handoff_ready.py
        │        asserts lint_bundle(bundle) == []  (each check_* fn)
        │        └─ caps verify --tier handoff  → LedgerEntry(tier="handoff", pytest)
        │
        └── live (profile-gated):  checks/prove_handoff_ready.py
                 require("APX_CAPS_PROFILE"); make_ws(profile)
                 read back: job run_as == SP? warehouse auto_stop? inventory?
                 [APX_CAPS_CANARY_ALLOW_MUTATE] clean bundle deploy + read back
                 proven()/disproven()/cannot_run()  → LedgerEntry(tier="handoff", shell)
```
- **New tracked file:** `python/src/apx_agent/_handoff_lint.py` (pure functions,
  PyYAML load, `list[str]` violations).
- **New (git-excluded) files:** `python/checks/prove_handoff_ready.py`,
  handoff entries in `python/capabilities.yaml`, fixture bundles under
  `python/tests/fixtures/handoff/` (a clean bundle + a violating bundle).
- **New test files:** `python/tests/test_handoff_ready.py` (cheap static) and
  `python/tests/test_handoff_ready_reality_ctk.py` (cheap read-back over fixtures
  + live-gated SP-owner read-back).
- **Edited (tracked):** `caps/manifest.py` (VALID_TIERS + DEFAULT_FRESHNESS),
  `caps/cli.py` (three `--tier` choices lists).

### `capabilities.yaml` shape (illustrative)
```yaml
- id: handoff-job-timeouts
  description: every job/task declares timeout_seconds
  given: a bundle databricks.yml
  when: the handoff static linter parses it
  then: no job/task is missing timeout_seconds
  tier: handoff
  check: tests/test_handoff_ready.py::test_all_jobs_have_timeouts   # pytest, freshness code
- id: handoff-owner-is-sp
  description: job/pipeline owner reads back as a service principal
  given: a target workspace (APX_CAPS_PROFILE)
  when: prove_handoff_ready reads each job run_as back live
  then: every run_as is a service principal, not a user
  tier: handoff
  freshness: 24h
  check: {shell: "python checks/prove_handoff_ready.py owner-is-sp"}
```

## Acceptance Criteria
### Cheap — static, CI-safe (pytest, verifiable=true)
- [ ] **AC-1 (cheap).** *Given* a fixture bundle where one job task omits
  `timeout_seconds`, *When* `_handoff_lint.check_job_timeouts(bundle)` runs,
  *Then* it returns a non-empty violation naming the job+task; a clean bundle
  returns `[]`. gate_file: `python/tests/test_handoff_ready.py`. gate_test:
  `test_all_jobs_have_timeouts`.
- [ ] **AC-2 (cheap).** *Given* a bundle with a warehouse lacking auto-stop and an
  endpoint lacking `scale_to_zero`, *When*
  `check_warehouse_autostop_and_scale_to_zero(bundle)` runs, *Then* it flags both
  by name; a compliant bundle returns `[]`. gate_file:
  `python/tests/test_handoff_ready.py`. gate_test: `test_warehouse_and_endpoint_idle`.
- [ ] **AC-3 (cheap).** *Given* a bundle whose `run_as`/owner is a personal
  `@databricks.com` identity, *When* `check_no_personal_owner(bundle)` runs,
  *Then* it flags that identity; an SP-owned bundle returns `[]`. gate_file:
  `python/tests/test_handoff_ready.py`. gate_test: `test_no_personal_owner`.
- [ ] **AC-4 (cheap).** *Given* a bundle referencing a bare/current-catalog table
  name, *When* `check_tables_fqn(bundle)` runs, *Then* it flags the non-FQN
  reference (static heuristic); fully-qualified `catalog.schema.table` references
  return `[]`. gate_file: `python/tests/test_handoff_ready.py`. gate_test:
  `test_tables_are_fully_qualified`.
- [ ] **AC-5 (cheap).** *Given* a bundle with a running (non-paused) schedule and
  no named owner, *When* `check_schedules_paused_or_owned(bundle)` runs, *Then* it
  flags it; a paused or owned schedule returns `[]`. gate_file:
  `python/tests/test_handoff_ready.py`. gate_test: `test_schedules_paused_or_owned`.
- [ ] **AC-6 (cheap).** *Given* the caps tier machinery, *When*
  `caps status/verify/add --tier handoff` is invoked and a handoff check runs,
  *Then* `handoff` is an accepted `--tier` value (choices at `caps/cli.py`
  :452/:459/:528), `VALID_TIERS`/`DEFAULT_FRESHNESS` include it
  (`caps/manifest.py:13`/:14), and a run writes a `LedgerEntry(tier="handoff")`.
  gate_file: `python/tests/test_handoff_ready.py`. gate_test:
  `test_handoff_tier_registered_and_ledgered`.
- [ ] **AC-7 (cheap, reality/Ctk read-after-write).** *Given* the violating and
  clean fixture bundles, *When* `lint_bundle` runs over each and the result is
  read back, *Then* the violating bundle yields REAL, non-empty violation strings
  that each name an offending path/identifier (asserted via
  `ctk.verify(Artifact(...))` on the collected report, not a bare count or
  exit-0) and the clean bundle yields `[]`. gate_file:
  `python/tests/test_handoff_ready_reality_ctk.py`. gate_test:
  `test_lint_report_is_real_not_exit0`.

### Live — read-back, profile-gated (verifiable=maybe / live-gated)
- [ ] **AC-8 (live, reality/Ctk read-after-write — REQUIRED read-back).** *Given* a
  target workspace (`APX_CAPS_PROFILE`) with handoff-ready jobs, *When*
  `prove_handoff_ready.py owner-is-sp` reads each job's `run_as` back live,
  *Then* every owner reads back as a **real service principal** (the read-back SP
  application_id/name is asserted via `ctk.verify(Artifact(...))` — a real fact,
  not exit-0), else `disproven`. gate_file:
  `python/tests/test_handoff_ready_reality_ctk.py`. gate_test:
  `test_live_owner_reads_back_as_sp`. **skip_reason:** requires `APX_CAPS_PROFILE`
  (live workspace + bundle); cheap AC-1..7 gate CI, this proves the real SP-owner
  fact. Live check: `python/checks/prove_handoff_ready.py owner-is-sp`.
- [ ] **AC-9 (live, opt-in mutate).** *Given* `APX_CAPS_PROFILE` **and**
  `APX_CAPS_CANARY_ALLOW_MUTATE=1`, *When* `prove_handoff_ready.py clean-deploy`
  runs a clean `bundle deploy` to a fresh target, *Then* the deploy succeeds and
  the created resources read back; without the mutate flag it `cannot_run`
  (exit 3). gate_file: `python/tests/test_handoff_ready_reality_ctk.py`.
  gate_test: `test_live_clean_deploy_and_readback`. **skip_reason:** requires
  `APX_CAPS_PROFILE` + `APX_CAPS_CANARY_ALLOW_MUTATE` (mutating deploy).
  Live check: `python/checks/prove_handoff_ready.py clean-deploy`.
- [ ] **AC-10 (live).** *Given* `APX_CAPS_PROFILE`, *When*
  `prove_handoff_ready.py idle-readback` reads warehouse auto-stop / endpoint
  `scale_to_zero` from the **live** definitions, *Then* each reads back enabled,
  else `disproven`. gate_file: `python/tests/test_handoff_ready_reality_ctk.py`.
  gate_test: `test_live_idle_reads_back`. **skip_reason:** requires
  `APX_CAPS_PROFILE`. Live check: `python/checks/prove_handoff_ready.py idle-readback`.
- [ ] **AC-11 (live).** *Given* `APX_CAPS_PROFILE`, *When*
  `prove_handoff_ready.py inventory` lists the created resources back, *Then* the
  live inventory is non-empty and matches the bundle's declared resource set,
  else `disproven`. gate_file: `python/tests/test_handoff_ready_reality_ctk.py`.
  gate_test: `test_live_inventory_lists_back`. **skip_reason:** requires
  `APX_CAPS_PROFILE`. Live check: `python/checks/prove_handoff_ready.py inventory`.

## Risks
- **FQN table detection is a static heuristic** — a `${var.catalog}.schema.table`
  reference or a table set at runtime can't be fully resolved statically.
  *Mitigation:* AC-4 flags bare single-segment / catalog-less refs only and marks
  the ceiling with a `ponytail:` comment; live AC-8/AC-11 cover the real facts.
- **No generic bundle parser exists** — the linter owns `databricks.yml` parsing
  (PyYAML). *Mitigation:* keep it a small pure module reused by both callers; if a
  real DABs parser lands later, swap `load_bundle` for it.
- **`verify --stale` re-proves only cheap** (`caps/cli.py:229`) — handoff-static
  is not auto-gated by a bare `verify`. *Mitigation:* CI/stopping-signal invoke
  `caps verify --tier handoff` explicitly (documented ceiling).
- **Live checks unprovable in PR CI** (no `APX_CAPS_PROFILE`). *Mitigation:*
  AC-8..11 are live-gated with `skip_reason`; cheap AC-1..7 are the merge gate —
  same boundary as tool-scoped-auth AC-8.
- **Mutating deploy (AC-9)** could touch a real workspace. *Mitigation:* double-
  gated (`APX_CAPS_PROFILE` + `APX_CAPS_CANARY_ALLOW_MUTATE`), deploy to a fresh
  throwaway target only, reusing the canary opt-in precedent.

## Open Questions
- [ ] Confirm the fresh-target strategy for AC-9's clean deploy — a per-run
  ephemeral bundle `target` + teardown-out-of-scope, or a dedicated sandbox
  target in the profile? Default: **ephemeral target, no teardown** (v1 out of
  scope for `bundle destroy`); escalate if a sandbox target must be provisioned.

---

## Agent Handoff
```json
{
  "prd_version": "1.0",
  "goal": "Add a 'handoff' proof tier to caps that proves a build is safe to hand over to a non-author: cheap static checks parse the bundle/databricks.yml in CI (every job/task has timeout_seconds; warehouses auto-stop and endpoints scale_to_zero; no personal @databricks.com owner; tables FQN-from-config; schedules paused or owned) and live profile-gated read-backs prove the facts (job/pipeline owner reads back as an SP; clean bundle deploy-and-read-back behind an opt-in mutate flag; warehouse auto-stop/scale_to_zero read back live; created-resource inventory lists back). Reuse the existing tier plumbing (verify/gate/ack/status/ledger) with no new command; the only new logic is a shared pure linter and one prove script. Proven by pytest + caps verify --tier handoff.",
  "success_criteria": ["'handoff' tier registered and runs through caps verify/gate/ack/status with ledger evidence", "cheap static checks parse the bundle and gate CI with no workspace", "live read-back checks prove SP ownership, clean deploy, idle warehouses/endpoints, and resource inventory against a profile", "opt-in mutate flag guards the deploy check like the canary precedent", "per-check ledger evidence identical to cheap/live", "no new command or report format"],
  "convergence": {
    "stopping_signal": "caps verify --tier handoff cheap checks green in CI AND cd python && uv run pytest -k handoff green AND make check green",
    "progress_metric": "failing handoff-check count",
    "known_ceiling": "live checks (AC-8..11) need APX_CAPS_PROFILE + a real bundle/workspace; without one they are local-only / waived in CI (same boundary as tool-scoped-auth AC-8). FQN table check is a static heuristic (can't resolve ${var}-catalog or runtime-set tables). verify --stale re-proves only cheap, so handoff-static is gated via explicit --tier handoff.",
    "re_represented": false
  },
  "acceptance_criteria": [
    { "id": "AC-1", "description": "check_job_timeouts flags a job/task missing timeout_seconds (names job+task); clean bundle -> []", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_handoff_ready.py", "gate_test": "test_all_jobs_have_timeouts" },
    { "id": "AC-2", "description": "check_warehouse_autostop_and_scale_to_zero flags a warehouse without auto-stop and an endpoint without scale_to_zero; compliant -> []", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_handoff_ready.py", "gate_test": "test_warehouse_and_endpoint_idle" },
    { "id": "AC-3", "description": "check_no_personal_owner flags a personal @databricks.com run_as/owner; SP-owned -> []", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_handoff_ready.py", "gate_test": "test_no_personal_owner" },
    { "id": "AC-4", "description": "check_tables_fqn flags a bare/current-catalog table reference (static heuristic); fully-qualified catalog.schema.table -> []", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_handoff_ready.py", "gate_test": "test_tables_are_fully_qualified" },
    { "id": "AC-5", "description": "check_schedules_paused_or_owned flags a running schedule with no named owner; paused or owned -> []", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_handoff_ready.py", "gate_test": "test_schedules_paused_or_owned" },
    { "id": "AC-6", "description": "'handoff' is an accepted --tier value (caps/cli.py:452/459/528), in VALID_TIERS+DEFAULT_FRESHNESS (caps/manifest.py:13/14), and a run writes LedgerEntry(tier='handoff')", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_handoff_ready.py", "gate_test": "test_handoff_tier_registered_and_ledgered" },
    { "id": "AC-7", "description": "Ctk read-after-write: lint_bundle over violating fixture yields REAL non-empty violations each naming an offending path/identifier (asserted via ctk.verify/Artifact, not count/exit-0); clean fixture -> []", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_handoff_ready_reality_ctk.py", "gate_test": "test_lint_report_is_real_not_exit0" },
    { "id": "AC-8", "description": "live Ctk read-after-write: prove_handoff_ready owner-is-sp reads each job run_as back live and asserts it is a REAL service principal (SP application_id/name via ctk.verify/Artifact, not exit-0), else disproven", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_handoff_ready_reality_ctk.py", "gate_test": "test_live_owner_reads_back_as_sp", "skip_reason": "requires APX_CAPS_PROFILE (live workspace+bundle); cheap AC-1..7 gate CI, this proves the real SP-owner read-back. Live check: python/checks/prove_handoff_ready.py owner-is-sp" },
    { "id": "AC-9", "description": "live opt-in mutate: with APX_CAPS_PROFILE + APX_CAPS_CANARY_ALLOW_MUTATE, a clean bundle deploy to a fresh target succeeds and resources read back; without the flag -> cannot_run (exit 3)", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_handoff_ready_reality_ctk.py", "gate_test": "test_live_clean_deploy_and_readback", "skip_reason": "requires APX_CAPS_PROFILE + APX_CAPS_CANARY_ALLOW_MUTATE (mutating deploy). Live check: python/checks/prove_handoff_ready.py clean-deploy" },
    { "id": "AC-10", "description": "live: prove_handoff_ready idle-readback reads warehouse auto-stop / endpoint scale_to_zero from the live definitions and asserts each enabled, else disproven", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_handoff_ready_reality_ctk.py", "gate_test": "test_live_idle_reads_back", "skip_reason": "requires APX_CAPS_PROFILE. Live check: python/checks/prove_handoff_ready.py idle-readback" },
    { "id": "AC-11", "description": "live: prove_handoff_ready inventory lists created resources back and matches the bundle's declared set (non-empty), else disproven", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_handoff_ready_reality_ctk.py", "gate_test": "test_live_inventory_lists_back", "skip_reason": "requires APX_CAPS_PROFILE. Live check: python/checks/prove_handoff_ready.py inventory" }
  ],
  "must_have": ["register 'handoff' in caps/manifest.py:13 VALID_TIERS + :14 DEFAULT_FRESHNESS('handoff':'code') and caps/cli.py --tier choices at :452/:459/:528 (status/verify/add)", "new tracked python/src/apx_agent/_handoff_lint.py: pure fns each returning list[str] violations (check_job_timeouts, check_warehouse_autostop_and_scale_to_zero, check_no_personal_owner, check_tables_fqn, check_schedules_paused_or_owned), lint_bundle, load_bundle(PyYAML) - imported by BOTH the pytest gate and the prove script", "python/checks/prove_handoff_ready.py using _live.py require/make_ws/proven/disproven/cannot_run/guard; subcommands owner-is-sp / clean-deploy / idle-readback / inventory; clean-deploy gated by require('APX_CAPS_CANARY_ALLOW_MUTATE') like prove_canary_split", "capabilities.yaml handoff entries: static as pytest node (freshness code), live as {shell: prove_handoff_ready ...} (freshness 24h)", "python/tests/test_handoff_ready.py (cheap static) + python/tests/test_handoff_ready_reality_ctk.py (cheap read-back over fixtures + live SP-owner read-back)", "fixture bundles python/tests/fixtures/handoff/{clean,violating}.yml", "no runner.py/ledger.py/gate/ack/status change - they key off tier/check_kind generically"],
  "out_of_scope": ["auto-remediation (report pass/fail, no fix)", "teardown execution (bundle destroy)", "config-surface export automation (Genie space export etc.)", "a separate 'caps handoff' command or bespoke report format"],
  "constraints": {
    "tech_stack": "Python, PyYAML, Databricks CLI/SDK, pytest, ctk, caps",
    "key_files": ["caps/manifest.py", "caps/cli.py", "python/src/apx_agent/_handoff_lint.py", "python/checks/prove_handoff_ready.py", "python/checks/_live.py", "python/capabilities.yaml", "python/tests/test_handoff_ready.py", "python/tests/test_handoff_ready_reality_ctk.py", "python/tests/fixtures/handoff/clean.yml", "python/tests/fixtures/handoff/violating.yml"],
    "patterns": "add 'handoff' to VALID_TIERS/DEFAULT_FRESHNESS (caps/manifest.py:13/14) and the three --tier choices lists (caps/cli.py:452/459/528) - runner/ledger/gate/status already generic over tier+check_kind; static check_kind='pytest', live check via {shell:...} (_parse_check caps/manifest.py:33); live prove script uses _live.py require/make_ws/proven/disproven/cannot_run/guard (exit 0/1/3) and gates mutation behind require('APX_CAPS_CANARY_ALLOW_MUTATE') like prove_canary_split.py; _handoff_lint fns return list[str] (NO tuple returns), empty=clean, each violation names offending path/identifier; parse databricks.yml with PyYAML (no generic parser in src); ledger evidence automatic via LedgerEntry(tier='handoff'); reality ACs assert real read-back via ctk.verify(Artifact(...)) not exit-0/.exists()",
    "lint_bans": "no .get(k,\"\"), no `x or \"\"`, no invented env defaults, no `object` annotations, no `tuple[...]` returns, no skipped tests"
  },
  "preferred_skills": ["ctk", "databricks-dabs", "databricks-unity-catalog"],
  "escalate_on": [
    "AC-9 fresh-target strategy requires provisioning a sandbox target rather than an ephemeral throwaway target",
    "FQN table static heuristic cannot distinguish a legitimately-resolved ${var.catalog} reference from a genuinely bare table for a required case",
    "a handoff live fact needs a workspace mutation beyond the double-gated clean deploy",
    "registering 'handoff' requires a change to runner.py/ledger.py/gate/ack/status logic (should not - escalate if it does)",
    "architectural decision not covered by this PRD"
  ],
  "loop_guards": {
    "max_iterations": 8,
    "state_hash_check": true,
    "heartbeat_interval_seconds": 30,
    "on_stuck": "pause_and_surface",
    "on_no_progress": "stop_and_escalate",
    "state_persistence": "local_disk"
  }
}
```
