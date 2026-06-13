# Apps Soak + Better Promote — Design Spec

**Date:** 2026-06-12
**Status:** Approved (design)
**Location:** `src/apx_agent/_canary_apps.py`, `src/apx_agent/cli.py`
**Related:** [apps-canary-hotswap-design.md](../../../docs/engine-scope/apps-canary-hotswap-design.md) · [apps-uc-registry-shim-design.md](../../../docs/engine-scope/apps-uc-registry-shim-design.md)

## Goal

Make the Databricks Apps **soak-then-promote** workflow trustworthy and one-command. Today the Apps target has no platform traffic split, so the soak pattern (a second App you load-test, then "promote the code" to prod) is the right shape — but two things undercut it:

1. **The canary is built by a different, thinner pipeline than prod**, so what you soak-test is not a faithful preview of what ships.
2. **Promote re-deploys prod from whatever tree the operator has checked out**, with a weak health check, no provenance link to the soaked artifact, and no durable rollback.

This spec fixes both: put the canary on the **same** deploy path as prod, then make `promote` provenance-exact, health-gated, auto-rolling-back, and recorded in the UC version ledger.

Not in scope: platform-level traffic split or blue-green-behind-a-router (Apps has neither; that's a separate, heavier design). This keeps the in-place prod swap, just makes it safe and faithful.

## The problem in detail

### Fidelity hole — the canary falls off the happy path

`_deploy_apps_impl` (the prod path) runs: preflight → optional resource-merge → **wheel build + `.build/` dependency-manifest staging (issue #116 — without it the container 502s)** → `bundle validate` → `bundle deploy` → `bundle run` → `_poll_app_ready` (ACTIVE/RUNNING) → experiment→SP grant → **`/readyz` capability gate** → UC manifest registration.

`deploy_canary_app` (today) runs only: write `canary-<v>` target → `bundle deploy` → `bundle run` → one `apps get` for the URL.

So the canary **skips** preflight, wheel/manifest staging, validate, readiness polling, the readyz gate, the SP grant, and UC registration. You can pass the canary and still have prod misbehave, because prod gets treatment the canary never did. The soak is a side road, not a checkpoint.

### Weak, manual promote

`promote_canary_app` re-deploys prod from the operator's current tree (`bundle deploy --target prod`), checks `apps get` state (not `/readyz`), then tears down the canary. No guarantee prod ships the soaked artifact; no auto-rollback; rollback is "re-deploy some old tree yourself."

## Design

### Phase 0 — One path: the canary IS the prod path, pointed at the canary target

Parameterize the happy path by target. `_deploy_apps_impl` already takes `bundle_target`; the gap is that `deploy_canary_app` reimplements a subset instead of calling it. Refactor so `apx canary deploy --target apps`:

1. Writes/refreshes the `canary-<v>` DAB target into `databricks.yml` (existing `add_canary_target_to_yml`).
2. Calls **the same** `_deploy_apps_impl` machinery with `bundle_target=canary-<v>` — so the canary gets the identical validate → wheel build → manifest staging → deploy → poll → readyz → UC-register sequence.

Result: the canary is a faithful preview. The readyz gate now runs on the canary at deploy time, and the UC manifest is registered for the canary (tagged `apx.serving=apps`, plus `apx.apps.role=canary`). There is exactly one deploy path; the soak cannot diverge from or bypass it.

**Seam note:** `_deploy_apps_impl` writes to stdout/stderr via a `log` callback and shells out via `_run_databricks_cmd`. Keep both seams; the canary caller passes the same `log` and the tests keep mocking `_run_databricks_cmd`. The canary-specific bits (target-name derivation, second-App naming, `traffic_hint` bookkeeping) stay in `_canary_apps.py` and wrap the shared impl.

### Phase 1 — Provenance via Databricks' own git capture

DAB records git metadata (`bundle.git.branch`, `bundle.git.commit`) on every `bundle deploy` run inside a repo — no manual SHA tracking. At canary deploy we read that SHA and stamp it onto:

- the canary's UC manifest **version tag** `apx.apps.git_sha`, and
- the canary App's env (`APX_GIT_SHA`) for runtime visibility.

The SHA is the artifact handle: "promote the exact soaked artifact" means "replay prod from this commit."

**Checkout for replay (v1 = local/CI; Repos = follow-up):** v1 assumes the promote runs where the repo is checked out (the dev box or CI), and the tool checks out the canary's SHA before deploying prod — the operator never hand-picks a commit. A follow-up adds a **Databricks Git folder (Repos)** path so the checkout is server-side (update the Repo to the SHA via the Repos API, deploy prod from it) for truly zero local git. v1 keeps it simple; the SHA capture + tag design is identical either way, so the follow-up is additive.

### Phase 2 — Better `promote`

`apx canary promote --target apps` (no args beyond optional `--keep-canary`) runs, aborting on any gate failure:

1. **Resolve** the canary's recorded SHA (UC manifest `apx.apps.git_sha` tag / canary App env).
2. **Gate IN** — canary must pass `/readyz` (reuse `_check_readyz`). Fail → abort, prod untouched.
3. **Capture rollback point** — record current prod's live SHA + UC version *before* any change.
4. **Replay** — check out the canary SHA, deploy prod via the shared `_deploy_apps_impl` (`bundle_target=prod`) — same faithful path.
5. **Gate OUT** — prod must pass `/readyz` after the swap. Fail → **auto-rollback** to the step-3 SHA (re-deploy prod from it), leave the canary intact, raise.
6. **Record** — register prod's UC manifest version for the promoted SHA; move the `@prod` UC alias to it. The previous version stays reachable = durable rollback handle.
7. **Teardown** the canary target (unless `--keep-canary`).

**Auto-rollback default: ON** (operator chose "readyz + auto-rollback"). A `--no-auto-rollback` flag leaves a failed prod in place for manual inspection (it still raises and keeps the canary).

### Rollback — two layers, both keyed on SHA

- **Automatic, in-promote:** post-swap readyz failure reverts prod to the pre-promote SHA. You never sit in a broken prod.
- **Durable, manual:** `apx canary rollback --target apps` re-points prod at the previous `@prod` UC version's recorded SHA. Because version + SHA live in UC, rollback is one command to a recorded version, not a tree hunt.

## Components

| Unit | Responsibility |
|---|---|
| `_deploy_apps_impl` (cli.py) | Unchanged contract; becomes the single deploy path both prod and canary call. |
| `deploy_canary_app` (`_canary_apps.py`) | Thin: write canary target, call `_deploy_apps_impl(target=canary-<v>)`, capture SHA, bookkeep. |
| `promote_canary_app` (`_canary_apps.py`) | The 7-step gate/replay/record/rollback sequence above. Keeps the `RunCmd` seam. |
| `rollback_canary_app` (`_canary_apps.py`) | Re-point prod at a recorded UC version's SHA. |
| New helpers | `_resolve_canary_sha`, `_current_prod_sha`, `_checkout_sha`, `set_apps_alias` (`@prod`). Reuse `_check_readyz`, `register_apps_manifest`. |

## Data flow

```
apx canary deploy --target apps --canary-version v42
  └─ add canary target → _deploy_apps_impl(target=canary-v42)
       → validate → build+stage → deploy → poll → readyz → register UC (role=canary, git_sha=<SHA>)
  ↳ soak: load-test the canary URL

apx canary promote --target apps
  └─ resolve canary SHA → readyz(canary)        [gate IN]
     → record prod SHA+version                  [rollback point]
     → checkout SHA → _deploy_apps_impl(target=prod)
     → readyz(prod) ── fail ──▶ redeploy prod@old SHA (auto-rollback), keep canary, raise
     └─ register prod UC version(git_sha), move @prod alias → teardown canary
```

## Error handling

- Any gate failure raises with an actionable message naming the failing capability (reuse the deploy's readyz error formatting that dumps app-log tails).
- Pre-promote: failures leave prod untouched and the canary intact.
- Post-swap: auto-rollback (default) or `--no-auto-rollback` leaves prod for inspection; canary always retained until prod is confirmed serving.
- UC writes (manifest/alias) are non-fatal post-success — same posture as the shim: a missing ledger entry never reddens a successful promote (warn + continue).

## Testing

Unit (mock `_run_databricks_cmd` / `RunCmd`, `_check_readyz`, MLflow client):
- Phase 0: `canary deploy` invokes the shared impl with `target=canary-<v>` and runs readyz + UC register (i.e. the canary no longer skips them).
- SHA capture: canary deploy stamps `apx.apps.git_sha` from DAB git metadata.
- Promote happy path: gate-IN pass → prod replay from canary SHA → gate-OUT pass → UC version + `@prod` alias moved → canary torn down.
- Gate-IN fail: prod untouched, no replay.
- Gate-OUT fail: auto-rollback redeploys prod@old-SHA, canary retained, raises; `--no-auto-rollback` skips the redeploy but still raises + retains.
- Rollback command: re-points prod at the previous version's SHA.

## Decided defaults (flag at spec review if wrong)

- **Repos/server-side checkout:** follow-up, not v1. v1 = local/CI checkout.
- **Auto-rollback:** ON by default; `--no-auto-rollback` to opt out.
- **In-place prod swap retained:** no blue-green router in this spec.

## Phasing

- **P0** — unify canary onto `_deploy_apps_impl` (faithful soak). Prerequisite; highest fidelity payoff.
- **P1** — git-SHA provenance capture (DAB git metadata → UC tag + app env).
- **P2** — gated/auto-rollback/UC-recorded promote + durable rollback.
