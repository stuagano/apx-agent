# Shimming Apps into the UC Model Registry — versioning design

**Status:** proposal · **Date:** 2026-06-12 · **Scope:** `apx-agent agents deploy --target apps`

The Apps deploy target has no version spine. Model Serving inherits one for free
from Unity Catalog: every `databricks.agents.deploy` mints a registered-model
version (`v1`, `v2`, …) with lineage, `apx.agent.*` tags, and per-version trace
correlation. Apps gets none of that — it ships a wheel into a container and the
"version" is, at best, a DAB target label.

This doc proposes a **shim**: register a UC model version on every Apps deploy
*without* promoting it to a serving endpoint. The UC registry becomes the
**version ledger and manifest** of what each App is running, even though the App —
not the registered artifact — serves the traffic.

It does **not** claim to deliver platform traffic-split canary for Apps. That gap
is real and stays out of scope (see §6). Sibling doc:
[apps-vs-model-serving.md](../deploy/apps-vs-model-serving.md).

---

## 1. Why this is feasible

The two halves of the Model-Serving flow are already decoupled in the code:

| Step | Function | Needs a serving endpoint? |
|---|---|---|
| Log + register artifact | `log_agent()` → `mlflow.pyfunc.log_model(registered_model_name=...)` | **No.** Returns a `registered_model_version`. |
| Promote to endpoint | `databricks.agents.deploy(model, model_version)` | Yes — this is the *only* serving-coupled step. |
| Write discovery tags | `set_uc_tags_for_agent()` → `client.set_registered_model_tag(...)` | **No.** |

The Apps path (`_deploy_apps`) skips all three. The shim simply runs the two
serving-independent ones (`log_agent`, `set_uc_tags_for_agent`) and *not*
`agents.deploy`. The deploy docstring already flags the gap this closes:

> Note: the auto-derived UC tags / publish-tools flow does not currently apply to
> `--target apps` (no model version to tag). Apps tagging will be addressed in a
> follow-up.

This *is* that follow-up.

## 2. What the shim buys

> **Conditional, on by default.** Registration runs on every `apx-agent agents deploy
> --target apps` **when a UC name and model are resolvable** — i.e. the project
> has `[tool.apx.agent].model` plus either `registered_model`, a non-placeholder
> `catalog`+`schema`, or an explicit `--uc-name`. A fresh scaffold ships
> `$CATALOG`/`$SCHEMA` placeholders, so an unconfigured project **skips with a
> loud, actionable notice** rather than registering under a bogus name — a bare
> apps deploy still succeeds. Configure UC once and every subsequent deploy mints
> a version.

- **A real version spine for Apps** — a configured `apx-agent agents deploy --target
  apps` mints a `v1/v2/v3` UC integer with lineage back to the run.
- **Discovery parity** — Apps agents finally appear in `apx-agent agents list`,
  topology, and the watchdog crawler, all of which read `apx.agent.*` UC tags.
- **Promote / rollback bookkeeping** via UC model **aliases** (`@prod`,
  `@canary`) — the alias records which version a live App is running.
- **`canary analyze` for free** — it already partitions off MLflow traces, so
  per-version latency/error comparison works *once the Apps runtime stamps the
  version onto its trace attributes* (§4).

## 3. The change to `_deploy_apps` (as implemented)

After the existing bundle deploy + poll-until-RUNNING, `_deploy_apps_impl` calls
`_register_apps_manifest_step(...)` (skipped under `--no-register-uc`). The tag
writes + `log_agent` live in `_apps_registry.register_apps_manifest`; the CLI
step owns resolution + skip/non-fatal policy:

```python
def _register_apps_manifest_step(*, module, config, app_name,
                                 bundle_target, uc_name_override, log):
    uc_name = _resolve_apps_uc_name(config, app_name, override=uc_name_override)
    if uc_name is None:
        log("# UC registration skipped: no UC model name resolved. Set "
            "[tool.apx.agent].registered_model (or a non-placeholder catalog + "
            "schema), or pass --uc-name, to enable the Apps version ledger. "
            "Pass --no-register-uc to silence this.")
        return                       # bare apps deploy still succeeds
    model = config.get("model")
    if not model:
        log("# UC registration skipped: no LLM model in [tool.apx.agent].model ...")
        return
    try:
        agent = _load_finalized_agent(module)
        from ._apps_registry import register_apps_manifest
        res = register_apps_manifest(agent, uc_name=uc_name, model=model,
                                     app_name=app_name, bundle_target=bundle_target,
                                     agent_name=config.get("name") or app_name)
        log(f"# registered {res.uc_name} version {res.version} "
            f"(manifest for App {res.app_name}; not promoted to serving)")
    except Exception as e:           # non-fatal: the App is already live
        log(f"# UC registration failed (non-fatal — App is live): {e}")
```

Key points:

- **Skip, don't error — but loudly.** When no UC name or model resolves, the step
  skips with an actionable stderr notice naming the fix. A bare `apx-agent agents
  deploy --target apps` must still exit 0; erroring on a default-on step would be
  a hostile regression. The notice (not silence) is what makes the skip correct —
  see [the anti-silent-failure principle in the dev-UI work].
- **Atomic with the deploy.** Register inside the same command, after the App is
  live, so the UC version and the running App stay in lockstep. A version logged
  independently invites drift (§6.2).
- **Non-fatal.** A registration failure logs a warning and the deploy still
  succeeds — the App is already serving; a missing ledger entry must not redden a
  green deploy.
- **`apx.serving=apps` tag** marks these versions as manifests, not
  serving-promoted — so `apx-agent agents list` / analyze can distinguish them and
  never try to `agents.deploy` them.
- **Opt-out flag** (`--no-register-uc`) for tight dev loops where the extra
  `log_model` packaging cost (§6.4) isn't worth it.

## 4. Per-version trace correlation

Model Serving gets version attribution free from served-entity names
(`<model>-<version>`). Apps has no served entity, so the runtime must stamp the
version itself.

1. At deploy, write the resolved version into an App env var:
   `APX_MODEL_VERSION=<n>` (via the DAB `--var` mechanism the hot-swap path
   already uses).
2. At startup, the runtime reads it and sets it on every trace as an audit
   attribute (extend the `apx.*` schema in `_audit.py` with `apx.model_version`).
3. `analyze_canary` partitions on `apx.model_version` when present, falling back
   to its current served-entity logic for Model Serving.

Without step 1–2, `analyze_canary` degrades to `versions=["unknown"]` for Apps —
the same graceful fallback it already has for older deployments.

## 5. Promote / rollback via aliases

UC model aliases give Apps the bookkeeping half of a promotion workflow:

- `apx-agent canary promote --target apps --canary-version <label>` → auto-resolves
  the latest canary version, then moves alias `@prod` → that version (and re-points
  the prod App's `APX_MODEL_VERSION` if needed).
- `apx-agent canary rollback --target apps --to-version <N>` → move `@prod` back to
  version N (`--to-version` is required).
- `canary status --target apps` reads aliases + `apx.apps.*` version tags to show
  which version each App is running.

The alias is the **source of truth for intent**; the running App is the
**fact**. Reconciling the two (App is running a version the alias doesn't point
at) is a useful `apx-agent doctor` check.

## 6. What this does NOT solve — read before building

1. **No traffic split.** The UC registry is a version *ledger*, not a router. An
   App serves 100% from one container; registering a version doesn't create a 10%
   canary. Real A/B for Apps stays a **two-App + external router** problem (the
   existing `canary-<label>` DAB approach). This shim fixes versioning,
   discovery, and lineage — not traffic shaping.
2. **The registered artifact isn't what executes.** The App runs your wheel /
   FastAPI directly; it does **not** load the pyfunc model from UC. The UC version
   is a *manifest/shadow* of the deploy, so it can drift if someone deploys the
   App without re-logging. Mitigation: keep register atomic with deploy (§3).
3. **Governance is still App-level for the running surface.** The UC version
   gets UC ACLs, but the thing actually serving traffic is the App, governed by
   App permissions. The shim improves auditability, not the runtime trust
   boundary.
4. **Cost.** `log_model` repackages deps and captures `uv.lock` on every Apps
   deploy — adds seconds-to-minutes. Default on, `--no-register-uc` to skip, and
   consider doing it async / post-deploy so it never blocks the App going live.

## 7. Phasing

- **P1 — ledger.** `log_agent` + `set_uc_tags` + `apx.serving=apps` version tags
  in `_deploy_apps`, behind `--no-register-uc` opt-out. Delivers versioning +
  discovery parity. No runtime changes.
- **P2 — correlation.** `APX_MODEL_VERSION` env var + `apx.model_version` audit
  attribute + `analyze_canary` partition support. Delivers per-version compare.
- **P3 — promotion.** Alias-based `promote`/`rollback`/`status` for `--target
  apps`, plus an `apx-agent doctor` reconcile check (alias vs. running App).

## 8. Open questions

- **UC naming for Apps.** Reuse `<catalog>.<schema>.<app_name>`, or a dedicated
  `apps` schema so manifest models are visibly separate from serving-promoted
  ones? Leaning dedicated schema for a clean `apx-agent agents list` filter.
- **Async logging.** Is post-deploy async registration worth the added failure
  mode (App live, UC version missing) versus blocking the deploy a bit longer?
  When that failure mode does hit, `apx-agent agents register` backfills the
  missing manifest for a single agent (issue #418).
- **Drift policy.** When `apx-agent doctor` finds App-vs-alias drift, warn only, or
  offer to re-point / re-register?
