# Canary & hot-swap across deploy targets — design

**Status:** implemented · **Scope:** `apx-agent canary *`, `apx-agent agents hot-swap`

apx-agent ships two deploy targets — Model Serving and Databricks Apps — with
different platform primitives underneath. Canary rollout and LLM hot-swap mean
different things on each. This doc is the rationale the source modules point at
(`_canary.py`, `_canary_apps.py`, `_hot_swap.py`, `_hot_swap_apps.py`, and the
`apx-agent canary` / `apx-agent agents hot-swap` CLI groups).

Related: [apps-vs-model-serving.md](../deploy/apps-vs-model-serving.md) ·
[apps-uc-registry-shim-design.md](./apps-uc-registry-shim-design.md).

---

## 1. The asymmetry

| Capability | Model Serving | Apps |
|---|---|---|
| Platform traffic split | **Yes** — one endpoint, N served entities, `TrafficConfig` routes a % to each | **No** — one App = one URL, serves 100% |
| Unit of a "version" | UC registered-model version (`v41`, `v42`) | DAB target / App (no native version) |
| Env change without re-log | **Yes** — rewrite `environment_vars` on the served entity | **No** — container env is fixed at deploy; re-deploy required |

Everything below follows from that split: on Model Serving we drive the platform
router; on Apps we emulate the *workflow* with a second App and re-deploys.

## 2. Canary — Model Serving (`_canary.py`)

Wraps `ws.serving_endpoints.update_config` + `TrafficConfig` + `Route` into four
moves keyed on real traffic split:

- **`deploy_canary(endpoint, new_version, traffic_pct=10)`** — add `new_version`
  as a second served entity; give it `traffic_pct`, redistribute the rest across
  existing entities proportionally.
- **`promote_canary(endpoint, version)`** — route 100% to `version`; leave the
  others configured so rollback is a traffic swap, not a re-add.
- **`rollback_canary(endpoint, version)`** — inverse of promote.
- **`analyze_canary(endpoint, experiment, lookback_hours=24)`** — walk MLflow
  traces over the window, partition by served-entity, return per-version request
  counts + latency P50/P95 + errors.

Per-version trace correlation relies on distinct served-entity names
(`<short_model>-<version>`). Older deployments without the served-entity
attribute fall back to `versions=["unknown"]` (workspace-wide breakdown).

## 3. Canary — Apps (`_canary_apps.py`)

Apps has no platform router, so the analog is a **soak environment**: a second
App off the same source tree under a deterministic target name, running beside
prod at its own URL.

- **`deploy_canary_app(canary_version, ...)`** — write a `canary-<version>` DAB
  target, then delegate the actual deploy to the **shared `_deploy_apps_impl`
  pipeline** (the full sequence: validate → wheel build → `.build/` manifest
  staging → poll for ACTIVE/RUNNING → `/readyz` capability gate → UC manifest
  registration). The canary target is selected via an injected `deploy_fn` and
  an `app_name_override` that routes the pipeline at the `canary-<v>` App name.
  Returns `AppsCanaryConfig` (prod/canary App names + URLs). `--traffic` is
  recorded as `traffic_hint` only — **not** a routing directive. The soak App
  is a faithful preview — it cannot diverge from or bypass the prod deploy path.
  See the [soak-promote design spec](../../python/docs/superpowers/specs/2026-06-12-apps-soak-promote-design.md).
- **`promote_canary_app(...)`** — re-deploy `prod` off the canary's source tree
  (optionally tear down the canary target). Source-control tagging is the
  operator's job; the module doesn't manage git.
- **`rollback_canary_app(version)`** — re-deploy `prod` off a prior tagged tree.
  Same code path as promote, inverse intent.
- **`analyze_canary_app(lookback_hours)`** — partition MLflow traces by the
  `apx.app.name` tag, return per-App requests + latency + errors
  (`AppsCanaryReport`).

The honest limitation: there is no 90/10 split. You get two live Apps and a
comparison; sending real users to one or the other is your router's problem, not
the platform's.

### 3a. Provenance + gate-don't-mutate promote/rollback (P1/P2)

The CLI `apx-agent canary {deploy,promote,rollback,status} --target apps` adds a
version ledger and a **gate-don't-mutate** workflow on top of the library
functions above (full design:
[apps-soak-promote-design.md](../../python/docs/superpowers/specs/2026-06-12-apps-soak-promote-design.md)):

- **deploy** runs the *same* `_deploy_apps_impl` path as prod (faithful soak)
  and stamps the deploy commit on the canary's UC manifest version as
  `apx.apps.git_sha` (P1 provenance).
- **promote** resolves the canary manifest, then **verifies the working tree is
  at the soaked commit and clean** — refusing with a `git checkout <sha>`
  instruction rather than ever mutating the tree — before re-deploying prod via
  the shared faithful path (readyz is the gate-OUT), moving the `@prod` UC
  alias, and tearing down the canary. Failure leaves `@prod` and the canary in
  place with a guided rollback hint.
- **rollback** is the same gate, addressed by `--to-version <N>`: it restores
  the commit a recorded version shipped (verify HEAD, redeploy, move `@prod`).
- **status** reads the ledger: the `@prod` version + commit and the latest
  canary version + commit.

Helpers live in `_apps_registry.py` (`find_latest_canary_version`,
`get_version_git_sha`, `get_prod_alias_version` / `set_prod_alias_version`,
`get_latest_apps_version`); the gate→deploy→alias step is shared between promote
and rollback (`cli._apps_deploy_prod_at_commit`). The standalone library
`promote_canary_app` / `rollback_canary_app` are unchanged (thin re-deploy off
the current tree) and remain available for programmatic callers.

## 4. Hot-swap — Model Serving (`_hot_swap.py`)

Change a deployed agent's **LLM** without re-logging the artifact. The runtime
(`_chat_agent.py::_resolve_model`) reads `APX_AGENT_MODEL_OVERRIDE` and prefers
it over the compile-time model. `hot_swap_model(endpoint, new_model)` rewrites
that env var on each served entity via `update_config_and_wait`, preserving
other env vars. Effect: next replica start picks up the new model — seconds-fast
rollback of a bad model change, or cheap model experiments without dozens of
artifact versions.

Caveats: takes effect on next replica start (scale-to-zero → next request cold
start picks it up; otherwise existing replicas serve the old model until they
cycle). Only honored by agents compiled through `compile_to_chat_agent`.

## 5. Hot-swap — Apps (`_hot_swap_apps.py`)

Apps' container env is fixed at deploy, so the analog re-deploys the bundle with
a different `--var llm_endpoint_name=NEW` (var name configurable,
`DEFAULT_LLM_VAR_NAME = "llm_endpoint_name"` — the scaffold convention). The App
restarts off the new env. `read_var_default` recovers the prior value from
`variables.<name>.default` (short and long YAML forms) so the result records
what changed.

This is heavier than the Model-Serving swap (a full re-deploy vs. an env
rewrite), which is inherent to the target.

## 6. CLI surface

- `apx-agent canary status --endpoint E` — served entities + traffic split.
- `apx-agent canary deploy [--target model-serving|apps] ...` — model-serving adds a
  served entity at N%; apps writes a `canary-<label>` target and deploys a
  sibling App.
- `apx-agent agents hot-swap [--target model-serving|apps] ...` — model-serving
  rewrites the override env var; apps re-deploys with a swapped bundle var.

`promote` / `rollback` / `analyze` follow the same `--target` convention,
dispatching to the matching module.

## 7. Why not unify them

The two targets genuinely differ at the platform layer — one has a traffic
router and mutable served-entity env, the other has neither. Hiding that behind
one abstraction would either over-promise (implying Apps can split traffic) or
under-deliver (refusing the soak-environment workflow that Apps *can* do). The
`--target` flag keeps the verbs the same while being honest that the semantics
differ. The version-ledger half of the gap — giving Apps real versions and
discovery — is addressed separately in
[apps-uc-registry-shim-design.md](./apps-uc-registry-shim-design.md).
