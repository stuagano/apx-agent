# Hub deploy on fevm — diagnosis (2026-09-25)

**Status:** Root cause is NOT the hub. A fresh, unmodified apx app fails to
deploy on the `fevm` workspace the same way. This is environmental / a current
platform-or-apx regression, not a hub bug. Two real hub fixes were made along
the way (below) — necessary, but not sufficient, because the blocker is upstream.

## The decisive experiment

A brand-new `apx init --addons ui` scaffold — zero hub code, untouched by us —
deployed to `fevm` and **failed identically**:

```
✓ App is started!
✓ Preparing source code for new app deployment.
✓ App deployment failed unexpectedly.
Error: failed to reach SUCCEEDED, got FAILED: App deployment failed unexpectedly.
```

- Fails in **~3–4 seconds**, at the **"Preparing source code" / source-prep**
  stage — **before** any `pip install` runs (install would take minutes).
- Same opaque message for the hub AND the fresh scaffold.
- **Conclusion: every newly-built apx UI app hits this on fevm right now.** It is
  not hub-specific, not caused by the reskin, not caused by the hub being stale,
  and not caused by anything we changed.

## Does this affect all agents?

**Newly-deployed ones on this workspace: yes, it appears so** (a clean scaffold
reproduces it). But note the split in what's already running on fevm:

- `apx-observability-fresh` — deployed from a `.build` with **source** inside
  (`agent_server/`, `pyproject.toml`, `uv.lock`) → SUCCEEDED (older build).
- `bordereaux-ops-agent` — deployed from **source-at-root** (`app.py`,
  `agent_server/`, bundled `apx_agent-0.5.0.whl`, `start.sh`) → SUCCEEDED.
- Current `apx build` (v0.3.8) produces a **wheel** `.build`
  (`<app>-*.whl` + `requirements.txt` + `app.yml`) — the hub's shape, and a
  fresh scaffold's shape. **This is the shape that now fails.**

So the working apps were deployed with an **earlier apx build model** (or an
earlier platform). Something changed — the current `apx build` output, the Apps
runtime's source-prep validation, or their interaction — such that the
wheel-based `.build` no longer deploys. That is the thing to chase, and it is
above the app layer.

## What to try (in order)

1. **Confirm on a second workspace / with a colleague.** Deploy a fresh
   `apx init --addons ui` scaffold elsewhere. If it also fails → current apx/
   platform regression, escalate to the apx team. If it works → something
   specific to `fevm`.
2. **Get the real error.** The failure reason is logged server-side but is not
   exposed via `databricks apps logs` (`--tail-lines` returns only a stale
   line), the deployment object, or the REST API. Needs: the Apps UI **events/
   system** view (not the deployment-logs tab), or apx/Databricks platform log
   access. Without it, everything below is hypothesis.
3. **Try the source-based `.build` shape** that a working app uses
   (`apx-observability-fresh`): ship source + `pyproject.toml` + `uv.lock` into
   `.build` instead of a wheel. If the current `apx build` can't emit that,
   that's the regression.
4. **Check apx version alignment.** Working apps used apx that produced
   different output; the installed `apx` is 0.3.8. A newer/older apx may build a
   `.build` the current platform accepts. The binary even prints "Upgrade apx to
   the latest version."

## Hub fixes made (real, keep — but not the deploy blocker)

Both committed; necessary for the hub regardless of the platform issue:

1. **`[tool.apx.ui] ui-root` → `root`** (merged, `68d7a772`). apx 0.3.8 renamed
   the key; the stale key made `apx build` write to a phantom `src/ui/` and
   regenerate `lib/api.ts` in the wrong place. Fixed — `apx build` now succeeds.
2. **Bundle apx-agent wheel + `start.sh`** (branch `hub-deploy-fix`, `5242b5db`).
   apx-agent is a local path dep (unpublished; Apps has no PyPI proxy). The
   working `bordereaux-ops-agent` bundles the wheel and `pip install`s it in
   `start.sh`. Applied the same. Correct, but the deploy fails before `start.sh`
   ever runs, so it didn't change the outcome.

## Mistakes we made (so you don't repeat them)

- `databricks workspace import-dir` **corrupts binary wheels** (base64/notebook
  encoding): a wheel round-tripped came back 1.33 MB vs 1.17 MB local. Use
  `databricks bundle deploy`'s file sync for binaries, never `import-dir`.
- Chased dependency-install and packaging-shape theories for a while; the
  ~4-second fail time (pre-install) and the fresh-scaffold repro are what finally
  showed it's environmental. Trust the timing signal early.

## State

- Repo clean. `hub-deploy-fix` branch holds fix #2 (unmerged).
- `agent-hub` app exists on `fevm` but has never deployed successfully.
- The `apxuiprobe` test app was deleted from `fevm`. Local `/tmp/apx-*probe`
  dirs can be removed (`rm -rf /tmp/apx-probe /tmp/apx-ui-probe /tmp/apx-wheel`).
