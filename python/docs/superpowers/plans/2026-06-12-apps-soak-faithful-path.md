# Apps Soak — Faithful Path (P0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `apx canary deploy --target apps` run the *exact same* deploy pipeline as prod (`_deploy_apps_impl`), pointed at the `canary-<v>` target, so the soak App is a faithful preview — same validate → wheel build → `.build/` manifest staging → poll → `/readyz` gate → UC registration.

**Architecture:** Today `deploy_canary_app` reimplements a thin subset of the deploy (deploy → run → one `apps get`) and skips the rest. P0 makes it **delegate the actual deploy to an injected `deploy_fn`** (the CLI passes one that calls `_deploy_apps_impl`), so there is one deploy path and the canary cannot diverge or bypass. Two small enablers: `_deploy_apps_impl` gains `app_name_override` (the canary target renames the App, so the shared path must poll/register the canary name, not the prod name), and `register_apps_manifest` gains `extra_version_tags` (so the canary's UC version can be tagged `apx.apps.role=canary`). Dependency injection keeps `_canary_apps.py` free of any `cli` import (no cycle).

**Tech Stack:** Python 3.11+, Click, pytest, Databricks Asset Bundles (DAB) via the `databricks` CLI (mocked in tests), MLflow `MlflowClient`.

**Spec:** [2026-06-12-apps-soak-promote-design.md](../specs/2026-06-12-apps-soak-promote-design.md) (this is Phase 0 of that spec; P1 provenance and P2 gated-promote are follow-on plans).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/apx_agent/cli.py` | `_deploy_apps_impl` — the single deploy path | Add `app_name_override` param; thread `extra_version_tags` into the UC step |
| `src/apx_agent/cli.py` | `_register_apps_manifest_step` | Accept + forward `extra_version_tags` |
| `src/apx_agent/cli.py` | `canary_deploy` (apps branch) | Build a `deploy_fn` over `_deploy_apps_impl` and pass it to `deploy_canary_app` |
| `src/apx_agent/_apps_registry.py` | `register_apps_manifest` | Accept `extra_version_tags`, write them as version tags |
| `src/apx_agent/_canary_apps.py` | `deploy_canary_app` | Stop shelling out directly; write the canary target, then delegate to `deploy_fn` |
| `tests/test_apps_registry.py` | registrar unit tests | Add: `extra_version_tags` written |
| `tests/test_deploy_apps.py` | deploy-path tests | Add: `app_name_override` polls/registers the override name |
| `tests/test_canary_apps.py` | canary unit tests | Rework `deploy_canary_app` tests to assert delegation to `deploy_fn` |

---

## Task 1: `register_apps_manifest` accepts `extra_version_tags`

**Files:**
- Modify: `src/apx_agent/_apps_registry.py`
- Test: `tests/test_apps_registry.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_apps_registry.py`:

```python
def test_register_writes_extra_version_tags(patched: dict[str, Any]) -> None:
    client = _FakeMlflowClient()
    register_apps_manifest(
        object(),
        uc_name="main.agents.my_app",
        model="m",
        app_name="my-app",
        bundle_target="canary-v42",
        mlflow_client=client,
        extra_version_tags={"apx.apps.role": "canary"},
    )
    tagged = {(key, value) for _n, _v, key, value in client.version_tags}
    assert ("apx.apps.role", "canary") in tagged
    # base manifest tags still written
    assert (SERVING_TAG, "apps") in tagged
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_apps_registry.py::test_register_writes_extra_version_tags -v`
Expected: FAIL — `register_apps_manifest() got an unexpected keyword argument 'extra_version_tags'`.

- [ ] **Step 3: Add the parameter and tag-write loop**

In `src/apx_agent/_apps_registry.py`, change the signature and the tag loop:

```python
def register_apps_manifest(
    agent: "BaseAgent",
    *,
    uc_name: str,
    model: str,
    app_name: str,
    bundle_target: str,
    agent_name: str | None = None,
    extra_version_tags: dict[str, str] | None = None,
    mlflow_client: Any | None = None,
) -> AppsManifestResult:
```

Then, after the three base tags are written, add:

```python
    for key, value in (extra_version_tags or {}).items():
        client.set_model_version_tag(uc_name, version, key, value)
```

Update the docstring `Args:` block to document `extra_version_tags` ("extra version-level tags, e.g. ``{'apx.apps.role': 'canary'}``").

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_apps_registry.py -v`
Expected: PASS (all registrar tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_apps_registry.py python/tests/test_apps_registry.py
git commit -m "feat(apps): register_apps_manifest accepts extra_version_tags"
```

---

## Task 2: `_deploy_apps_impl` accepts `app_name_override`

**Files:**
- Modify: `src/apx_agent/cli.py` (`_deploy_apps_impl`, `_register_apps_manifest_step`)
- Test: `tests/test_deploy_apps.py`

Context: under a canary target, `add_canary_target_to_yml` overrides `resources.apps.<key>.name` to `canary_app_name(...)`, but `_resolve_app_name(doc)` reads the *base* name (prod). The shared path must poll / readyz / register the override name when one is supplied.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_deploy_apps.py`:

```python
def test_app_name_override_polls_override_name(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When app_name_override is set, `apps get` targets the override name."""
    from apx_agent import cli as cli_mod

    calls = _install_subprocess_mock(monkeypatch)
    # Call the impl directly with an override; capture nothing else changes.
    logs: list[str] = []
    cli_mod._deploy_apps_impl(
        cwd=scaffold, module="agent:agent", profile=None,
        bundle_target="canary-v42", no_run=False, auto_update_yml=False,
        auto_build_wheel=False, auto_experiment=False, vars=(),
        json_output=False, readyz_gate=False, register_uc=False,
        uc_name=None, app_name_override="my-app-canary-v42",
        log=logs.append,
    )
    # The `apps get` poll used the override name, not the base "my-app".
    get_calls = [c for c in calls if c[:2] == ["apps", "get"]]
    assert get_calls, "expected an apps get call"
    assert all("my-app-canary-v42" in c for c in get_calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_deploy_apps.py::test_app_name_override_polls_override_name -v`
Expected: FAIL — `_deploy_apps_impl() got an unexpected keyword argument 'app_name_override'`.

- [ ] **Step 3: Add the parameter and use it for the app name**

In `src/apx_agent/cli.py`, add to `_deploy_apps_impl`'s keyword args (next to `uc_name`):

```python
    app_name_override: str | None = None,
```

Then change the app-name resolution. Find:

```python
    bundle_key, app_name = _resolve_app_name(doc)
```

Replace with:

```python
    bundle_key, resolved_app_name = _resolve_app_name(doc)
    app_name = app_name_override or resolved_app_name
    if app_name_override and app_name_override != resolved_app_name:
        log(f"# app-name override: polling {app_name} (target {bundle_target})")
```

`bundle_key` still comes from the doc (used by `bundle run`); only the workspace App name we `apps get` / readyz / register is overridden.

- [ ] **Step 4: Thread the override into `_deploy_apps` wrapper**

In `_deploy_apps`, add `app_name_override: str | None = None` to its signature and pass it through to `_deploy_apps_impl(... app_name_override=app_name_override ...)`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_deploy_apps.py::test_app_name_override_polls_override_name -v`
Expected: PASS.

- [ ] **Step 6: Run the existing deploy tests to confirm no regression**

Run: `cd python && uv run pytest tests/test_deploy_apps.py -q`
Expected: PASS (all, including the 4 UC-registration tests).

- [ ] **Step 7: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_deploy_apps.py
git commit -m "feat(apps): _deploy_apps_impl accepts app_name_override for canary targets"
```

---

## Task 3: Thread `extra_version_tags` through the UC step

**Files:**
- Modify: `src/apx_agent/cli.py` (`_deploy_apps_impl`, `_register_apps_manifest_step`)
- Test: `tests/test_deploy_apps.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_deploy_apps.py`:

```python
def test_register_uc_forwards_extra_version_tags(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """extra_version_tags passed to _deploy_apps_impl reach register_apps_manifest."""
    (scaffold / "pyproject.toml").write_text(_PYPROJECT_WITH_AGENT)
    seen: dict[str, Any] = {}

    def _fake_registrar(agent, *, uc_name, model, app_name, bundle_target,
                        agent_name=None, extra_version_tags=None):
        seen["tags"] = extra_version_tags
        from apx_agent._apps_registry import AppsManifestResult
        return AppsManifestResult(uc_name=uc_name, version="1", app_name=app_name)

    monkeypatch.setattr("apx_agent._apps_registry.register_apps_manifest", _fake_registrar)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda m: object())
    _install_subprocess_mock(monkeypatch)

    from apx_agent import cli as cli_mod
    cli_mod._deploy_apps_impl(
        cwd=scaffold, module="agent:agent", profile=None, bundle_target="canary-v42",
        no_run=False, auto_update_yml=False, auto_build_wheel=False,
        auto_experiment=False, vars=(), json_output=False, readyz_gate=False,
        register_uc=True, uc_name="main.agents.my_app",
        app_name_override="my-app-canary-v42",
        extra_version_tags={"apx.apps.role": "canary"},
        log=lambda *_a: None,
    )
    assert seen["tags"] == {"apx.apps.role": "canary"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_deploy_apps.py::test_register_uc_forwards_extra_version_tags -v`
Expected: FAIL — `_deploy_apps_impl() got an unexpected keyword argument 'extra_version_tags'`.

- [ ] **Step 3: Add `extra_version_tags` to the impl and the step**

In `_deploy_apps_impl` signature add:

```python
    extra_version_tags: dict[str, str] | None = None,
```

In the register call site (the `if register_uc:` block), pass it:

```python
        _register_apps_manifest_step(
            module=module,
            config=_read_apx_agent_config(),
            app_name=app_name,
            bundle_target=bundle_target,
            uc_name_override=uc_name,
            extra_version_tags=extra_version_tags,
            log=log,
        )
```

In `_register_apps_manifest_step`, add the parameter and forward it:

```python
def _register_apps_manifest_step(
    *,
    module: str,
    config: dict[str, Any],
    app_name: str,
    bundle_target: str,
    uc_name_override: str | None,
    extra_version_tags: dict[str, str] | None = None,
    log: Any,
) -> None:
```

and in the `register_apps_manifest(...)` call inside it, add `extra_version_tags=extra_version_tags`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_deploy_apps.py::test_register_uc_forwards_extra_version_tags -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_deploy_apps.py
git commit -m "feat(apps): thread extra_version_tags through the UC registration step"
```

---

## Task 4: `deploy_canary_app` delegates to an injected `deploy_fn`

**Files:**
- Modify: `src/apx_agent/_canary_apps.py` (`deploy_canary_app`)
- Test: `tests/test_canary_apps.py`

The canary stops shelling out to `bundle deploy/run/get` itself. It writes the canary target, then calls `deploy_fn` — which the CLI supplies as a wrapper over `_deploy_apps_impl`. This is the heart of P0: one deploy path.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_canary_apps.py` (a new test that asserts delegation):

```python
def test_deploy_canary_app_delegates_to_deploy_fn(tmp_path: Path) -> None:
    from apx_agent import _canary_apps

    yml = tmp_path / "databricks.yml"
    yml.write_text(
        "resources:\n  apps:\n    my-app:\n      name: my-app\n"
        "targets:\n  prod:\n    default: true\n"
    )
    seen: dict[str, Any] = {}

    def fake_deploy_fn(*, bundle_target, app_name_override, extra_version_tags):
        seen.update(
            bundle_target=bundle_target,
            app_name_override=app_name_override,
            extra_version_tags=extra_version_tags,
        )

    cfg = _canary_apps.deploy_canary_app(
        cwd=tmp_path,
        bundle_key="my-app",
        base_app_name="my-app",
        canary_version="v42",
        traffic_hint=10,
        deploy_fn=fake_deploy_fn,
    )
    # Delegated to the shared path with the canary target + canary app name.
    assert seen["bundle_target"] == "canary-v42"
    assert seen["app_name_override"] == "my-app-canary-v42"
    assert seen["extra_version_tags"] == {"apx.apps.role": "canary"}
    # The canary target was written into the yml.
    assert "canary-v42" in yml.read_text()
    # Result still reports prod + canary identities.
    assert cfg.prod_app_name == "my-app"
    assert cfg.canary_app_name == "my-app-canary-v42"
```

Note: the asserted strings are the verified helper outputs — `canary_target_name('v42') == 'canary-v42'`, `canary_app_name('my-app','v42') == 'my-app-canary-v42'`, `sanitize_version('v42') == 'v42'`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_canary_apps.py::test_deploy_canary_app_delegates_to_deploy_fn -v`
Expected: FAIL — `deploy_canary_app() got an unexpected keyword argument 'deploy_fn'` (it still takes `run_cmd`).

- [ ] **Step 3: Rewrite `deploy_canary_app` to delegate**

Replace the body of `deploy_canary_app` in `src/apx_agent/_canary_apps.py`. New signature + body:

```python
# Type alias for the injected deploy seam — the CLI passes a wrapper over
# _deploy_apps_impl so this module never imports cli (no cycle).
DeployFn = Callable[..., Any]


def deploy_canary_app(
    *,
    cwd: Path,
    bundle_key: str,
    base_app_name: str,
    canary_version: str,
    traffic_hint: int,
    deploy_fn: "DeployFn",
    base_target: str = "prod",
) -> AppsCanaryConfig:
    """Write the canary target, then deploy it through the SHARED deploy path.

    ``deploy_fn`` is the prod deploy pipeline (``_deploy_apps_impl``) injected
    by the CLI. Delegating to it — instead of shelling out to a thin
    deploy/run/get subset here — is what makes the soak App a faithful preview:
    it gets the same validate → wheel build → manifest staging → poll → readyz
    → UC registration that prod gets. See
    docs/superpowers/specs/2026-06-12-apps-soak-promote-design.md (Phase 0).
    """
    target_name = canary_target_name(canary_version)
    new_app_name = canary_app_name(base_app_name, canary_version)

    import yaml
    yml_path = cwd / "databricks.yml"
    doc = yaml.safe_load(yml_path.read_text()) or {}
    add_canary_target_to_yml(
        doc, bundle_key=bundle_key, base_app_name=base_app_name,
        version=canary_version, base_target=base_target,
    )
    write_databricks_yml(yml_path, doc)

    deploy_fn(
        bundle_target=target_name,
        app_name_override=new_app_name,
        extra_version_tags={"apx.apps.role": "canary"},
    )

    return AppsCanaryConfig(
        bundle_target=target_name,
        canary_version=sanitize_version(canary_version),
        prod_app_name=base_app_name,
        canary_app_name=new_app_name,
        canary_app_url="",  # URL now surfaced by the shared deploy path's output
        traffic_hint=traffic_hint,
    )
```

Remove the now-unused `run_cmd`/`profile` params, the direct `bundle deploy/run`, and the `apps get` URL read from this function. (The shared path prints the canary URL.) Leave `_prod_is_serving`, `promote_canary_app`, `rollback_canary_app`, and the yml helpers untouched in this task — promote is P2.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_canary_apps.py::test_deploy_canary_app_delegates_to_deploy_fn -v`
Expected: PASS.

- [ ] **Step 5: Update obsolete `deploy_canary_app` tests**

Any existing `test_canary_apps.py` test that drove `deploy_canary_app` with a `run_cmd` (asserting it issued `bundle deploy --target canary-...`) is now testing behavior that moved to the shared path. For each such test: either delete it (the assertion is covered by Task 6's CLI test) or convert it to the `deploy_fn` delegation shape above. Run `cd python && uv run pytest tests/test_canary_apps.py -q` and fix each failure by updating the test to the new contract. Do NOT weaken assertions to pass — delete tests whose subject no longer exists, convert tests whose subject moved.

- [ ] **Step 6: Commit**

```bash
git add python/src/apx_agent/_canary_apps.py python/tests/test_canary_apps.py
git commit -m "refactor(apps): deploy_canary_app delegates to injected deploy_fn (one path)"
```

---

## Task 5: Wire the CLI `canary_deploy` apps branch to the shared path

**Files:**
- Modify: `src/apx_agent/cli.py` (`canary_deploy`, apps branch)
- Test: `tests/test_deploy_apps.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_deploy_apps.py` (drives the real CLI; asserts the canary runs the full path against the canary app name):

```python
def test_canary_deploy_apps_uses_full_path(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`apx canary deploy --target apps` runs validate + deploy + poll against
    the canary target and the canary app name — i.e. the faithful path."""
    calls = _install_subprocess_mock(monkeypatch)
    result = CliRunner().invoke(main, [
        "canary", "deploy", "--target", "apps", "--canary-version", "v42",
    ])
    assert result.exit_code == 0, result.output
    seq = [c for c in calls]
    # Deployed under the canary target.
    assert any(c[:2] == ["bundle", "deploy"] and "canary-v42" in c for c in seq), seq
    # Validate ran (the thin canary path used to skip it).
    assert any(c[:2] == ["bundle", "validate"] for c in seq), seq
    # Polled the CANARY app name, not prod "my-app".
    get_calls = [c for c in seq if c[:2] == ["apps", "get"]]
    assert get_calls and all("my-app-canary-v42" in c for c in get_calls), get_calls
    # canary target written into the bundle.
    assert "canary-v42" in (scaffold / "databricks.yml").read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_deploy_apps.py::test_canary_deploy_apps_uses_full_path -v`
Expected: FAIL — current apps branch calls the old `deploy_canary_app(run_cmd=...)` (no validate; polls prod name), so one of the asserts fails.

- [ ] **Step 3: Rewrite the apps branch of `canary_deploy`**

In `src/apx_agent/cli.py`, locate the `--target apps` branch of `canary_deploy` (after the `model-serving` branch returns). Replace its body so it builds a `deploy_fn` over `_deploy_apps_impl` and passes it to `deploy_canary_app`:

```python
    # --target apps
    if not canary_version:
        raise click.UsageError("--canary-version is required for --target apps.")
    from apx_agent import deploy_canary_app

    cwd = Path.cwd()
    doc = _read_databricks_yml(cwd)
    bundle_key, base_app_name = _resolve_app_name(doc)

    def log(msg: str) -> None:
        click.echo(msg, err=True)

    def _deploy_fn(*, bundle_target: str, app_name_override: str,
                   extra_version_tags: dict[str, str]) -> None:
        _deploy_apps_impl(
            cwd=cwd,
            module="agent:agent",
            profile=profile,
            bundle_target=bundle_target,
            no_run=False,
            auto_update_yml=False,
            auto_build_wheel=True,
            auto_experiment=True,
            vars=(),
            json_output=False,
            readyz_gate=True,
            register_uc=True,
            uc_name=None,
            app_name_override=app_name_override,
            extra_version_tags=extra_version_tags,
            log=log,
        )

    cfg = deploy_canary_app(
        cwd=cwd,
        bundle_key=bundle_key,
        base_app_name=base_app_name,
        canary_version=canary_version,
        traffic_hint=traffic_pct,
        deploy_fn=_deploy_fn,
        base_target=base_target,
    )
    click.echo(f"Canary App deployed: {cfg.canary_app_name}")
    click.echo(f"  prod App:   {cfg.prod_app_name}")
    click.echo(f"  soak via the full deploy path (validate→build→readyz→UC).")
    return
```

Note: `_resolve_app_name` returns an `_AppNameResolution`; unpack via its attributes if it is not directly tuple-unpackable (`res = _resolve_app_name(doc); bundle_key, base_app_name = res.bundle_key, res.app_name`). Match the unpacking style already used in `_deploy_apps_impl`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_deploy_apps.py::test_canary_deploy_apps_uses_full_path -v`
Expected: PASS.

- [ ] **Step 5: Run the full canary + deploy test set**

Run: `cd python && uv run pytest tests/test_deploy_apps.py tests/test_canary_apps.py tests/test_apps_registry.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_deploy_apps.py
git commit -m "feat(apps): canary deploy runs the full _deploy_apps_impl path (faithful soak)"
```

---

## Task 6: Docs — record the path unification

**Files:**
- Modify: `docs/engine-scope/apps-canary-hotswap-design.md`

- [ ] **Step 1: Update the Apps-canary section**

In `docs/engine-scope/apps-canary-hotswap-design.md` §3 (Canary — Apps), replace the description of `deploy_canary_app` shelling out to its own deploy with: the canary now runs the **same `_deploy_apps_impl` path as prod** (validate → build → manifest staging → poll → readyz → UC register), pointed at the `canary-<v>` target via an injected `deploy_fn` and `app_name_override`. Add one sentence: "The soak App is a faithful preview — it cannot diverge from or bypass the prod deploy path." Link the spec.

- [ ] **Step 2: Commit**

```bash
git add docs/engine-scope/apps-canary-hotswap-design.md
git commit -m "docs(apps): canary deploy now uses the faithful shared path"
```

---

## Task 7: Full verification gate

- [ ] **Step 1: pyright (gated modules must stay clean)**

Run: `cd python && uv run pyright src/apx_agent/`
Expected: `0 errors`. (`cli.py` and `_canary_apps.py` are in the type-debt exclude list, but `_apps_registry.py` is gated — keep it clean.)

- [ ] **Step 2: Full test suite**

Run: `cd python && uv run pytest -q`
Expected: all pass (no new failures vs. the 2218-passed baseline).

- [ ] **Step 3: Commit any final fixups, then stop for review**

The plan is complete when both gates are green. Do not open a PR automatically — hand back for review.

---

## Out of scope (follow-on plans, per the spec)

- **P1 — provenance:** capture the canary's git SHA from DAB git metadata (`bundle.git.commit`), stamp `apx.apps.git_sha` (via the `extra_version_tags` seam built here) + an `APX_GIT_SHA` app env.
- **P2 — better promote:** gate-IN (canary readyz) → record prod rollback point → replay prod from the canary SHA via the shared path → gate-OUT + auto-rollback → register prod UC version + move `@prod` alias → teardown. Plus durable `rollback` to a recorded version's SHA.
