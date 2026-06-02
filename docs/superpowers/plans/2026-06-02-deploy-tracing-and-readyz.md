# Deploy Tracing + Readiness Self-Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. TDD throughout: `cd python && uv run pytest`.

**Goal:** Make MLflow tracing work out-of-the-box in deployed Databricks Apps, and add a real readiness self-test (`/readyz`) that `apx deploy` gates on — so a green deploy proves the agent actually answers, traces, and (best-effort) runs a tool.

**Architecture:** Three independent slices. Slice A (tracing-on) is the foundation and ships first. Slice B adds the `/readyz` framework endpoint. Slice C makes `apx deploy` call `/readyz` and fail loudly. Each slice is a separate PR, merge-commit to main, full suite + pyright as the gate.

**Tech Stack:** Python, click CLI, FastAPI, MLflow tracing (`mlflow.langchain.autolog`), Databricks SDK/CLI, langgraph.

**Background (proven live on fe-cowork, 2026-06-02):** Deployed apps recorded **0 traces** for two compounding reasons:
1. Autolog is gated behind `APX_AGENT_MLFLOW_AUTOLOG`; `apx run` sets it but deploy never did, and the scaffold `start_server.py` never calls `autolog_if_env()`.
2. `apx deploy` creates the tracing experiment under the **deploying user** (`/Users/<user>/<bundle>-<target>`), but the app runs as its **service principal**, which can't access it → `WARNING mlflow.tracing.fluent: Failed to start span … experiment … does not exist`. Granting the app SP `CAN_MANAGE` on the experiment made a trace land (verified). FEVM trace *export* works once the grant is in place.

---

## Slice A — Tracing on by default in deployed Apps

**Files:**
- Modify: `python/src/apx_agent/cli.py` — `_SCAFFOLD_APPS_START_SERVER` (~line 660), `_SCAFFOLD_APPS_DATABRICKS_YML` env block (~line 829), `_deploy_apps_impl` (post-poll, ~line 2871), new helper `_grant_experiment_to_sp`.
- Test: `python/tests/test_cli.py`.

### Task A1: Scaffold `start_server.py` enables autolog

- [ ] **Step 1 — failing test.** In `tests/test_cli.py`:
```python
def test_scaffold_apps_start_server_enables_autolog() -> None:
    from apx_agent.cli import _SCAFFOLD_APPS_START_SERVER
    assert "autolog_if_env" in _SCAFFOLD_APPS_START_SERVER
    # called before the user agent import (so spans capture the first run)
    s = _SCAFFOLD_APPS_START_SERVER
    assert s.index("autolog_if_env()") < s.index("from agent import agent")
```
- [ ] **Step 2** — run it; expect FAIL.
- [ ] **Step 3 — implement.** In `_SCAFFOLD_APPS_START_SERVER`, after the `from apx_agent import compile_to_responses_agent, mount_mcp_endpoints` line, insert:
```python
from apx_agent._mlflow_tracing import autolog_if_env

# Enable MLflow LangChain/LangGraph auto-tracing when APX_AGENT_MLFLOW_AUTOLOG
# is set (databricks.yml sets it on deploy). Must run before the agent's
# LangChain components are built/invoked.
autolog_if_env()
```
  (Place it BEFORE `from agent import agent`.) Reference: the validated edit already applied to `~/Documents/apx-cowork-validation/cowork-validation/agent_server/start_server.py`.
- [ ] **Step 4** — run test; expect PASS.
- [ ] **Step 5 — commit.** `git add python/src/apx_agent/cli.py python/tests/test_cli.py`

### Task A2: Scaffold `databricks.yml` sets `APX_AGENT_MLFLOW_AUTOLOG=1`

- [ ] **Step 1 — failing test:**
```python
def test_scaffold_apps_databricks_yml_enables_autolog_env() -> None:
    from apx_agent.cli import _SCAFFOLD_APPS_DATABRICKS_YML
    assert "APX_AGENT_MLFLOW_AUTOLOG" in _SCAFFOLD_APPS_DATABRICKS_YML
```
- [ ] **Step 2** — FAIL.
- [ ] **Step 3 — implement.** In `_SCAFFOLD_APPS_DATABRICKS_YML`, in the app `env:` list (after the `MLFLOW_EXPERIMENT_ID` entry), add:
```yaml
        - name: APX_AGENT_MLFLOW_AUTOLOG
          value: "1"
```
- [ ] **Step 4** — PASS.
- [ ] **Step 5 — commit.**

### Task A3: `apx deploy` grants the app SP access to the tracing experiment

The load-bearing fix. The app SP is in the `_poll_app_ready` payload (`service_principal_client_id`). The experiment id is the resolved `mlflow_experiment_id` (from `extra_vars` or the `--var`). Grant via the permissions API; idempotent.

- [ ] **Step 1 — failing test** (mock subprocess to assert the grant call shape):
```python
def test_grant_experiment_to_sp_issues_patch(monkeypatch) -> None:
    from apx_agent import cli
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    cli._grant_experiment_to_sp("2960967542309513", "ff83b07a-2ab4-4564-88d4-54fb79417b06", profile="fe-cowork")
    flat = " ".join(" ".join(c) for c in calls)
    assert "/api/2.0/permissions/experiments/2960967542309513" in flat
    assert "ff83b07a-2ab4-4564-88d4-54fb79417b06" in flat
```
- [ ] **Step 2** — FAIL.
- [ ] **Step 3 — implement** `_grant_experiment_to_sp(experiment_id, sp_client_id, *, profile)` in `cli.py`. Build an ACL JSON `{"access_control_list":[{"service_principal_name": sp_client_id, "permission_level":"CAN_MANAGE"}]}`, write to a temp file, and run `databricks api patch /api/2.0/permissions/experiments/<id> --json @<tmp> [--profile <p>]`. Best-effort: on non-zero exit, `click.echo` a warning (don't fail the deploy). Verified working live via this exact endpoint/ACL.
- [ ] **Step 4 — wire into `_deploy_apps_impl`** after the poll returns `payload` (~line 2871) and only when an experiment id was resolved. Extract `sp = payload.get("service_principal_client_id")`; if `sp` and the resolved experiment id both exist, call `_grant_experiment_to_sp(exp_id, sp, profile=profile)` and `log("  granted app SP CAN_MANAGE on tracing experiment")`. (Thread the resolved experiment id down to this point — capture it where `extra_vars` gets `mlflow_experiment_id=`.)
- [ ] **Step 5** — run test; PASS. Run `uv run pytest tests/test_cli.py -q`.
- [ ] **Step 6 — commit.**

### Slice A gate
- [ ] `cd python && uv run pytest -q` (full suite) + `uv run pyright src/apx_agent/`.
- [ ] Restore `uv.lock` if poisoned (`git checkout -- uv.lock`), targeted `git add` only.
- [ ] PR → CI green → merge-commit to main.
- [ ] **Live re-verify:** bump `cowork-validation` lock to the new main, redeploy with a *clean* `start_server.py`/`databricks.yml` regenerated from the scaffold, invoke, confirm a trace lands **without** a manual grant.

---

## Slice B — `/readyz` capability self-test endpoint

**Files:**
- Create: `python/src/apx_agent/_readyz.py` — `mount_readyz(app, agent, *, model)`.
- Modify: `python/src/apx_agent/__init__.py` — export `mount_readyz`.
- Modify: `python/src/apx_agent/cli.py` — `_SCAFFOLD_APPS_START_SERVER` calls `mount_readyz(app, agent)` next to `mount_mcp_endpoints(app, agent)`.
- Test: `python/tests/test_readyz.py`.

### Design
`GET /readyz` runs a canned prompt through a freshly compiled agent and returns structured JSON. Checks:
- `llm` — agent produced a non-empty assistant message for the canned prompt (`"Reply with exactly: READY"`).
- `tracing` — a trace was recorded for the run (capture via `mlflow.get_last_active_trace_id()` / search the active experiment immediately after; `ok` iff a trace id is produced).
- `tools_registered` — count of tools on the agent (informational).
- `tool_exec` — **best-effort**: skipped by default (running a real tool needs OBO/user data access the SP self-test lacks). Reported as `"skipped"` unless `?tools=1` is passed and a no-arg tool exists.

HTTP 200 with `{"status":"ready", "checks":{...}}` when `llm` and `tracing` pass; HTTP 503 `{"status":"degraded", ...}` otherwise. Always wrap in try/except → never 500.

### Task B1: `mount_readyz` with the llm + tracing checks
- [ ] **Step 1 — failing test** (`tests/test_readyz.py`): build a tiny `Agent` with a stub model, `mount_readyz` onto a FastAPI app, `TestClient.get("/readyz")`, assert JSON has `status` and `checks.llm`/`checks.tracing`, and that a passing run → 200. Mock the compile/invoke so no network. (Mirror patterns in `tests/test_responses_agent.py` / `tests/test_managed_mcp.py`.)
- [ ] **Step 2** — FAIL.
- [ ] **Step 3 — implement** `mount_readyz(app, agent, *, model=None)`: register a `GET /readyz` route that compiles the agent (reuse `compile_to_responses_agent` or the langgraph compile), invokes the canned prompt, inspects the response + last trace id, and returns the JSON/HTTP described above. No-op friendly when mlflow absent (tracing → `"unavailable"`).
- [ ] **Step 4** — PASS.
- [ ] **Step 5 — commit.**

### Task B2: export + scaffold wiring
- [ ] Export `mount_readyz` from `apx_agent/__init__.py` (add to `__all__`). Test: `from apx_agent import mount_readyz`.
- [ ] In `_SCAFFOLD_APPS_START_SERVER`, add `from apx_agent import ..., mount_readyz` and `mount_readyz(app, agent)` right after `mount_mcp_endpoints(app, agent)`. Test asserts both substrings present.
- [ ] Update the scaffold docstring bullet list to mention `/readyz`.
- [ ] **Commit.**

### Slice B gate
- [ ] Full suite + pyright. PR → CI → merge. Restore uv.lock.
- [ ] **Live:** redeploy `cowork-validation`, `GET /readyz` with SP/user token → assert `status: ready`, `checks.tracing` ok.

---

## Slice C — `apx deploy` gates on `/readyz`

**Files:**
- Modify: `python/src/apx_agent/cli.py` — `_deploy_apps_impl` (after poll + URL known), new flag `--readyz-gate/--no-readyz-gate` (default on), helper `_check_readyz(app_url, profile)`.
- Test: `python/tests/test_cli.py`.

### Task C1: `_check_readyz` + deploy gate
- [ ] **Step 1 — failing test**: mock the HTTP GET to `<app_url>/readyz`; assert deploy raises `click.ClickException` when status != ready, and succeeds when ready; assert `--no-readyz-gate` skips the call.
- [ ] **Step 2** — FAIL.
- [ ] **Step 3 — implement** `_check_readyz(app_url, *, profile)`: GET `<app_url>/readyz` with a bearer token from `databricks auth token [--profile]` (reuse existing token-fetch pattern; never log the token). Parse JSON; return (ok, checks).
- [ ] **Step 4 — wire** into `_deploy_apps_impl` after the app URL is known (end of deploy), gated on `readyz_gate`. On not-ready, raise `click.ClickException` with the failing checks rendered. Add the `--readyz-gate/--no-readyz-gate` click option (default True) to the deploy command.
- [ ] **Step 5** — PASS. Full suite.
- [ ] **Step 6 — commit.**

### Slice C gate
- [ ] Full suite + pyright. PR → CI → merge. Restore uv.lock.
- [ ] **Live:** redeploy `cowork-validation` → deploy now self-verifies `/readyz` before declaring success.

---

## Self-review checklist
- Tracing requires BOTH autolog-on (A1/A2) AND the SP experiment grant (A3) — neither alone suffices (proven live).
- `/readyz` tracing check must not require the manual grant — Slice A's deploy grant must land first (sequence A → B → C).
- `tool_exec` is best-effort: do NOT fail `/readyz`/the deploy gate on it (OBO/data coupling). Say so in the JSON (`"skipped"`).
- Never print tokens (CLAUDE.md). Targeted `git add`; restore `uv.lock` each slice.
- Commit messages end `Co-authored-by: Isaac`; PR bodies end `This pull request and its description were written by Isaac.`
