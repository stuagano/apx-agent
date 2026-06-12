# Apps → UC Registry Shim (P1: version ledger) — Implementation Plan

> **Status: IMPLEMENTED 2026-06-12.** Shipped as `_apps_registry.py` +
> `_resolve_apps_uc_name` / `_register_apps_manifest_step` in `cli.py`, with
> `--register-uc/--no-register-uc` + `--uc-name` flags. Tests in
> `tests/test_apps_registry.py` (4) and `tests/test_deploy_apps.py` (4 new).
> One deviation from the original sketch, on advisor review: unresolved UC
> name/model **skips with a loud, actionable notice** rather than raising
> `UsageError`, so a bare `apx agents deploy --target apps` still succeeds.
> Registration is therefore on-by-default *when a UC name + model are configured*
> — see the conditional-behavior note in
> [the design doc §2](../../../docs/engine-scope/apps-uc-registry-shim-design.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `apx agents deploy --target apps` a real version spine by registering a UC model version on every Apps deploy — *without* promoting it to a serving endpoint. The UC registry becomes the version ledger + manifest of what each App is running, and Apps agents start appearing in `apx agents list` / topology / watchdog.

**Scope:** P1 only — the ledger. Per-version trace correlation (P2) and alias-based promote/rollback (P3) are deferred; see [apps-uc-registry-shim-design.md](../../../docs/engine-scope/apps-uc-registry-shim-design.md) §7.

**Design source:** [apps-uc-registry-shim-design.md](../../../docs/engine-scope/apps-uc-registry-shim-design.md).

**Architecture:** `log_agent()` (`_chat_agent.py`) and `databricks.agents.deploy()` are already decoupled — the first logs + registers a UC model version and returns it; the second is the only serving-coupled step. The Apps path (`_deploy_apps_impl`, `cli.py:4605`) skips both. P1 adds the register + tag half (never `agents.deploy`) after the App is live, behind a `--no-register-uc` opt-out, marking the version `apx.serving=apps` so nothing downstream tries to promote it.

**Tech Stack:** apx-agent, MLflow (`mlflow.pyfunc.log_model`, `MlflowClient`), Click CLI, pytest.

---

## File Map

| File | Responsibility | Change |
|------|---------------|--------|
| `src/apx_agent/cli.py` | `_deploy_apps` / `_deploy_apps_impl` (4563/4605), `deploy` command | Add `register_uc` param + register/tag step; thread `--register-uc/--no-register-uc` flag |
| `src/apx_agent/_apps_registry.py` | **New** — `register_apps_manifest(agent, *, uc_name, model, app_name, bundle_target, ws)` | Encapsulate log_agent + version tags + set_uc_tags so cli.py stays thin and the logic is unit-testable |
| `src/apx_agent/__init__.py` | Public exports | Export `register_apps_manifest` |
| `tests/test_apps_registry.py` | **New** — unit tests for the manifest registrar (mock MLflow) | — |
| `tests/test_deploy_apps.py` | Existing Apps deploy tests | Add: register step runs by default, skipped with `--no-register-uc`, never calls `agents.deploy` |

---

## Task 1: Manifest registrar module

**Files:**
- Create: `src/apx_agent/_apps_registry.py`

- [ ] **Step 1: Write `register_apps_manifest`**

Reuses the serving-independent halves already proven in the model-serving path:

```python
"""Register a UC model version as a *manifest* of a deployed Databricks App.

The App runs the wheel directly; it does NOT load this pyfunc model. The
registered version is a version-ledger record — "App <name> deploy corresponds
to this logged artifact" — tagged apx.serving=apps so downstream tooling never
tries to agents.deploy it. See docs/engine-scope/apps-uc-registry-shim-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agents import BaseAgent


@dataclass(frozen=True)
class AppsManifestResult:
    uc_name: str
    version: str
    app_name: str


def register_apps_manifest(
    agent: "BaseAgent",
    *,
    uc_name: str,
    model: str | None,
    app_name: str,
    bundle_target: str,
    agent_name: str | None = None,
    mlflow_client: Any | None = None,
) -> AppsManifestResult:
    import mlflow
    from mlflow.tracking import MlflowClient

    from ._chat_agent import log_agent
    from ._watchdog import set_uc_tags_for_agent

    with mlflow.start_run():
        info = log_agent(agent, model=model, registered_model_name=uc_name)
    version = info.registered_model_version

    client = mlflow_client or MlflowClient()
    for key, value in (
        ("apx.serving", "apps"),
        ("apx.apps.app_name", app_name),
        ("apx.apps.bundle_target", bundle_target),
    ):
        client.set_model_version_tag(uc_name, version, key, value)

    set_uc_tags_for_agent(agent, registered_model_name=uc_name,
                          model=model, name=agent_name)
    return AppsManifestResult(uc_name=uc_name, version=version, app_name=app_name)
```

- [ ] **Step 2: Export from `__init__.py`** — add `register_apps_manifest` and `AppsManifestResult` to the public surface (match the existing canary/hot-swap export style).

## Task 2: UC-name resolution for Apps

**Files:**
- Modify: `src/apx_agent/cli.py`

- [x] **Step 1: `_resolve_apps_uc_name(config, app_name, *, override)`**

Resolution order: explicit `--uc-name` → `[tool.apx.agent].registered_model` → `<catalog>.<schema>.<app_name>` composed from config (top-level or `template`), with the App name sanitized (hyphens → underscores) and `$CATALOG`/`$SCHEMA` placeholders treated as absent. Returns `None` when nothing resolves — **the caller skips with a loud notice, not a `UsageError`** (decision changed on advisor review: erroring on a default-on step breaks bare apps deploys). (Open question in the design doc §8: dedicated `apps` schema vs. reuse — reuse for P1.)

## Task 3: Wire into the Apps deploy path

**Files:**
- Modify: `src/apx_agent/cli.py` (`_deploy_apps`, `_deploy_apps_impl`, `deploy` command)

- [ ] **Step 1: Add `register_uc: bool = True` and `uc_model_name: str | None`** params to `_deploy_apps` / `_deploy_apps_impl`.

- [ ] **Step 2: Call the registrar after the App is ACTIVE/RUNNING**, only when `register_uc`:

```python
if register_uc:
    uc_name = uc_model_name or _resolve_apps_uc_name(config, app_name)
    from apx_agent import register_apps_manifest
    res = register_apps_manifest(
        agent, uc_name=uc_name, model=model,
        app_name=app_name, bundle_target=bundle_target,
        agent_name=effective_agent_name,
    )
    click.echo(f"Registered {res.uc_name} version {res.version} "
               f"(manifest for App {res.app_name}; not promoted to serving)")
```

Register **after** the App is live so a registration failure never blocks the deploy — emit a warning and continue (match the `publish-tools` failure handling at cli.py:3542).

- [ ] **Step 3: Add the CLI flag** — `--register-uc/--no-register-uc` (default `True`) and `--uc-name` on the `deploy` command; thread both into `_deploy_apps`. Document in the command help that Apps registration is a manifest, not a serving promotion.

- [ ] **Step 4: Update the deploy docstring** — remove the "does not currently apply to `--target apps`" note (cli.py:3403) now that it does.

## Task 4: Tests

**Files:**
- Create: `tests/test_apps_registry.py`
- Modify: `tests/test_deploy_apps.py`

- [ ] **Step 1: Unit tests for `register_apps_manifest`** (mock `log_agent`, `MlflowClient`, `set_uc_tags_for_agent`):
  - logs once, returns the version from `log_agent`
  - writes all three `apx.serving` / `apx.apps.*` version tags
  - calls `set_uc_tags_for_agent` with the resolved uc_name
  - never imports/calls `databricks.agents`

- [ ] **Step 2: Integration-ish tests in `test_deploy_apps.py`** (existing subprocess/seam mocks):
  - register step runs by default after deploy
  - `--no-register-uc` skips it entirely
  - registrar failure logs a warning but the deploy still reports success
  - `agents.deploy` is never invoked on the Apps path

- [ ] **Step 3: Run the suite** — `cd python && uv run pytest tests/test_apps_registry.py tests/test_deploy_apps.py -q` then full `uv run pytest -q` + `uv run pyright` before pushing (per repo convention).

## Task 5: Docs

**Files:**
- Modify: `docs/deploy/apps-vs-model-serving.md`

- [ ] **Step 1: Update the governance + versioning rows** — note that `--target apps` now mints a UC version manifest with `apx.agent.*` tags (discovery parity), while clarifying it's still not serving-promoted and has no traffic split. Link the design doc.

---

## Out of scope (follow-ups)

- **P2 — correlation:** `APX_MODEL_VERSION` bundle var + `apx.model_version` audit attribute in `_audit.py::AuditAttrs` + `analyze_canary_app` partition support, so per-version compare works for Apps.
- **P3 — promotion:** alias-based (`@prod`/`@canary`) `promote`/`rollback`/`status` for `--target apps`, plus an `apx doctor` reconcile check (alias intent vs. running App fact).
- **Traffic split:** explicitly *not* addressed — remains a two-App + external router concern (see [apps-canary-hotswap-design.md](../../../docs/engine-scope/apps-canary-hotswap-design.md) §3).
