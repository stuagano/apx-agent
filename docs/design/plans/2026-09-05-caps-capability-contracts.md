# Capability contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Ctk/caps prove apx-agent's public runtime promises with 12 cheap CI caps and seven live Databricks caps, after the harness itself is tested.

**Architecture:** Harden the vendored `caps/` runner, gate, doctor, and hooks first (tier filter, cheap-only local blocking, waiver preservation, Cursor `stop` adapter, `python/` uv checks). Then add missing MLflow-eval and SQL-cancel reality tests. Then declare capabilities via `python -m caps add` (never hand-edit `capabilities.yaml`). Cheap verify runs in PR CI; live verify is a separate workflow that never runs on pull requests.

**Tech Stack:** Python 3.11+, pytest, Ctk (`ctk.run` / `claim_vs_reality`), vendored `caps/` + PyYAML, `uv --frozen` in `python/`, GitHub Actions, Cursor hooks schema `version: 1`.

**Spec:** [docs/design/caps-capability-contracts.md](../caps-capability-contracts.md)

## Global Constraints

- Caps are product promises; tests are evidence, not 1:1 manifest entries.
- Cheap and live proofs are separate entries.
- No skipped proof: missing live config is `error` / unproven, never pass.
- Live checks require `APX_CAPS_PROFILE`; ambient `DATABRICKS_CONFIG_PROFILE` is not accepted.
- Never auto-select a Databricks CLI profile.
- Live workspace checks never run in pull-request CI.
- Do not auto-revert files after a failed proof.
- Do not commit credentials, hostnames, resource IDs, generated `python/examples/**/build/`, or unrelated dirty files (`python/uv.lock` unless this work requires `pyyaml`).
- Never hand-edit `capabilities.yaml`; use `python -m caps add`.
- Cheap checks: `cd python && uv run --frozen pytest <nodes> -q`.
- Cursor `stop` emits bounded `followup_message` only; CI is the hard gate.
- `docs/superpowers/` is gitignored; this plan lives in tracked `docs/design/plans/`.

## File map

- Create: `python/tests/test_caps_kit.py` — harness tests (tier, waiver, skip, gate JSON, root resolution, doctor defaults).
- Create: `.cursor/hooks.json`, `.cursor/hooks/caps-stop-gate.sh` — Cursor `stop` adapter.
- Create: `python/tests/test_sql.py` additions inside existing `TestRunSql` — statement cancel wiring.
- Create: `python/tests/test_dev_ui_eval_mlflow_reality_ctk.py` — eval GET uses MLflow when experiment is set.
- Create: `checks/_live.py` and `checks/prove_*.py` — seven live probes.
- Create: `.github/workflows/caps-live.yml` — scheduled / `workflow_dispatch` live verify only.
- Modify: `caps/cli.py` — `--tier` on `status`/`verify`; waiver re-read; Cursor gate stdout.
- Modify: `caps/gate.py` — `resolve_root` uses `workspace_roots` / `CURSOR_PROJECT_DIR`; `decide` blocks cheap caps only.
- Modify: `caps/doctor.py` — project `.claude/settings.json` and `.cursor/hooks.json`.
- Modify: `caps/state.py` — no change to `BLOCK_STATES` unless tests require documenting live vs cheap in `decide`.
- Modify: `python/src/apx_agent/_sql.py` — cancel statement on cancel token.
- Modify: `python/pyproject.toml` — add `pyyaml` to `dependency-groups.dev` if `import yaml` fails in the uv env.
- Modify: `Makefile`, `.github/workflows/ci.yml` — `python -m caps verify --tier cheap` after pytest (code PRs only).
- Modify: `.gitignore` — ignore `.ctk/ledger.json`.
- Modify: `capabilities.yaml` — only through `caps add`.
- Modify: `CLAUDE.md` / `AGENTS.md` — one paragraph: cheap vs live, `APX_CAPS_PROFILE`, no skip-as-pass.

Do not touch `python/examples/databricks-tools-core/build/`, `daily-summaries/`, or other pre-existing dirty paths.

---

### Task 1: Caps harness tests and `--tier`

**Files:**
- Create: `python/tests/test_caps_kit.py`
- Modify: `caps/cli.py` (`cmd_status`, `cmd_verify`, `main` argparse)
- Modify: `caps/gate.py` (`decide` cheap-only blocking)
- Modify: `python/pyproject.toml` (add `pyyaml` under `[dependency-groups] dev` if needed)
- Test: `python/tests/test_caps_kit.py`

**Interfaces:**
- Consumes: `caps.cli.main`, `caps.cli.cmd_verify`, `caps.cli.cmd_status`, `caps.gate.decide`, `caps.runner.run_capability`, `caps.manifest.Capability` / `load_manifest`, `caps.freshness.waiver_active`
- Produces: `cmd_status(..., tier: str | None = None)`, `cmd_verify(..., tier: str | None = None)`, `main` flags `--tier {cheap,live}`; `decide` blocks only `cap.tier == "cheap"` members of `BLOCK_STATES`

- [ ] **Step 1: Write failing tests**

At the top of `python/tests/test_caps_kit.py`:

```python
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from caps.cli import main
from caps.freshness import waiver_active
from caps.gate import decide, resolve_root
from caps.ledger import LedgerEntry, load_ledger
from caps.runner import run_capability
from caps.manifest import Capability, load_manifest


def _manifest(*entries: str) -> str:
    body = "\n".join(entries)
    return f"capabilities:\n{body}\n"


def _cheap_shell(tmp_path: Path, script: str = "exit 0") -> str:
    sh = tmp_path / "ok.sh"
    sh.write_text("#!/bin/sh\n" + script + "\n")
    sh.chmod(0o755)
    return str(sh)


def test_empty_manifest_loads_zero(tmp_path: Path) -> None:
    (tmp_path / "capabilities.yaml").write_text("capabilities:\n")
    assert load_manifest(tmp_path / "capabilities.yaml") == []


def test_malformed_manifest_raises(tmp_path: Path) -> None:
    (tmp_path / "capabilities.yaml").write_text("capabilities: {}\n")
    with pytest.raises(Exception):
        load_manifest(tmp_path / "capabilities.yaml")


def test_all_skipped_pytest_is_error(tmp_path: Path) -> None:
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "test_skip.py").write_text(
        "import pytest\n\ndef test_x():\n    pytest.skip('no infra')\n"
    )
    cap = Capability(
        id="skippy",
        description="d",
        given="g",
        when="w",
        then="t",
        tier="cheap",
        deps=[],
        freshness="code",
        check_kind="pytest",
        check_target="checks/test_skip.py::test_x",
        warnings=[],
    )
    result, _detail, _dur = run_capability(cap, tmp_path)
    assert result == "error"


def test_verify_tier_cheap_skips_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ok = tmp_path / "ok.sh"
    ok.write_text("#!/bin/sh\nexit 0\n")
    ok.chmod(0o755)
    boom = tmp_path / "boom.sh"
    boom.write_text("#!/bin/sh\nexit 1\n")
    boom.chmod(0o755)
    (tmp_path / "capabilities.yaml").write_text(
        _manifest(
            f"  - id: cheap-ok\n    description: d\n    given: g\n    when: w\n    then: t\n"
            f"    tier: cheap\n    deps: []\n    check:\n      shell: {ok}\n",
            f"  - id: live-boom\n    description: d\n    given: g\n    when: w\n    then: t\n"
            f"    tier: live\n    deps: []\n    check:\n      shell: {boom}\n",
        )
    )
    monkeypatch.chdir(tmp_path)
    rc = main(["verify", "--tier", "cheap"], cwd=str(tmp_path))
    assert rc == 0
    ledger = load_ledger(tmp_path / ".ctk" / "ledger.json")
    assert ledger["cheap-ok"].result == "pass"
    assert "live-boom" not in ledger


def test_decide_blocks_unproven_cheap_not_live(tmp_path: Path) -> None:
    ok = tmp_path / "ok.sh"
    ok.write_text("#!/bin/sh\nexit 0\n")
    ok.chmod(0o755)
    (tmp_path / "capabilities.yaml").write_text(
        _manifest(
            f"  - id: cheap-a\n    description: d\n    given: g\n    when: w\n    then: t\n"
            f"    tier: cheap\n    deps: []\n    check:\n      shell: {ok}\n",
            f"  - id: live-a\n    description: d\n    given: g\n    when: w\n    then: t\n"
            f"    tier: live\n    deps: []\n    check:\n      shell: {ok}\n",
        )
    )
    now = datetime.now(UTC)
    d = decide({"cwd": str(tmp_path)}, now)
    assert d.block is True
    assert d.reason is not None
    assert "cheap-a" in d.reason
    assert "live-a" not in d.reason


def test_ack_survives_full_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    boom = tmp_path / "boom.sh"
    boom.write_text("#!/bin/sh\nexit 1\n")
    boom.chmod(0o755)
    (tmp_path / "capabilities.yaml").write_text(
        _manifest(
            f"  - id: live-flaky\n    description: d\n    given: g\n    when: w\n    then: t\n"
            f"    tier: live\n    deps: []\n    check:\n      shell: {boom}\n",
        )
    )
    monkeypatch.chdir(tmp_path)
    assert main(["ack", "live-flaky", "--reason", "infra down"], cwd=str(tmp_path)) == 0
    assert main(["verify"], cwd=str(tmp_path)) == 0
    entry = load_ledger(tmp_path / ".ctk" / "ledger.json")["live-flaky"]
    assert waiver_active(entry, datetime.now(UTC))
    assert entry.result == "waived"


def test_verify_capability_overrides_waiver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    boom = tmp_path / "boom.sh"
    boom.write_text("#!/bin/sh\nexit 1\n")
    boom.chmod(0o755)
    (tmp_path / "capabilities.yaml").write_text(
        _manifest(
            f"  - id: live-flaky\n    description: d\n    given: g\n    when: w\n    then: t\n"
            f"    tier: live\n    deps: []\n    check:\n      shell: {boom}\n",
        )
    )
    monkeypatch.chdir(tmp_path)
    main(["ack", "live-flaky", "--reason", "infra down"], cwd=str(tmp_path))
    rc = main(["verify", "--capability", "live-flaky"], cwd=str(tmp_path))
    assert rc == 1
    entry = load_ledger(tmp_path / ".ctk" / "ledger.json")["live-flaky"]
    assert entry.result == "fail"
    assert entry.waiver is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && uv run --frozen pytest tests/test_caps_kit.py -q --tb=short`

Expected: FAIL — `--tier` is not a `verify` argument; `decide` currently blocks every `BLOCK_STATES` cap including live `never-proven`; full `verify` may overwrite waivers.

If `ModuleNotFoundError: yaml`, add `pyyaml` to `python/pyproject.toml` `[dependency-groups] dev` and `uv lock` in `python/` only.

- [ ] **Step 3: Minimal implementation**

In `caps/cli.py`, add `--tier` to `status` and `verify` parsers (`choices=["cheap", "live"]`, default `None`). Change signatures:

```python
def cmd_status(root: Path, now: datetime, as_json: bool = False, check: bool = False, tier: str | None = None) -> int:
    caps = load_manifest(root / MANIFEST_NAME)
    if tier is not None:
        caps = [c for c in caps if c.tier == tier]
    ...


def cmd_verify(
    root: Path, now: datetime, only: str | None, stale: bool = False, tier: str | None = None
) -> int:
    caps = load_manifest(root / MANIFEST_NAME)
    if only is None and tier is not None:
        caps = [c for c in caps if c.tier == tier]
    ...
```

Before running each cap in `cmd_verify`, re-load disk ledger and skip if `only is None and waiver_active(disk.get(cap.id), now)`. Keep the existing merge-at-save that preserves waivers.

In `main`, pass `args.tier` into `cmd_status` and `cmd_verify`.

In `caps/gate.py` `decide`, after computing `state`:

```python
        if cap.tier == "live":
            if state == "time-expired":
                expired.append(cap)
            continue
        if state in BLOCK_STATES:
            blocking.append((cap, state, entry))
        elif state == "time-expired":
            expired.append(cap)
```

Cheap `time-expired` should not occur (freshness is `code`); keep the branch for safety.

- [ ] **Step 4: Re-run tests**

Run: `cd python && uv run --frozen pytest tests/test_caps_kit.py -q --tb=short`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add python/tests/test_caps_kit.py caps/cli.py caps/gate.py python/pyproject.toml python/uv.lock
git commit -m "test(caps): prove tier filter, skip-as-error, and waiver preserve"
```

Omit `python/uv.lock` / `pyproject.toml` if PyYAML was already importable.

---

### Task 2: Cursor root resolution, gate JSON, doctor

**Files:**
- Modify: `caps/gate.py` (`resolve_root`)
- Modify: `caps/cli.py` (`cmd_gate`)
- Modify: `caps/doctor.py` (`diagnose` default settings + Cursor hook)
- Modify: `python/tests/test_caps_kit.py`
- Create: `.cursor/hooks.json`
- Create: `.cursor/hooks/caps-stop-gate.sh`

**Interfaces:**
- Consumes: `GateDecision`, `decide`, Cursor stdin fields `workspace_roots`, `status`, `loop_count`, `hook_event_name`
- Produces: `resolve_root` order: `workspace_roots` paths containing `capabilities.yaml`, then `CURSOR_PROJECT_DIR`, `cwd`, `transcript_path` parents; `cmd_gate` Cursor completed+block → `{"followup_message": reason}`; abort/error/exhausted loop → `{}`; Claude Stop → existing `{"decision":"block","reason":...}`

- [ ] **Step 1: Write failing tests**

Append to `python/tests/test_caps_kit.py`:

```python
def test_resolve_root_uses_workspace_roots(tmp_path: Path) -> None:
    (tmp_path / "capabilities.yaml").write_text("capabilities:\n")
    root = resolve_root({"workspace_roots": [str(tmp_path)]})
    assert root == tmp_path.resolve()


def test_resolve_root_uses_cursor_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "capabilities.yaml").write_text("capabilities:\n")
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(tmp_path))
    root = resolve_root({})
    assert root == tmp_path.resolve()


def test_cmd_gate_cursor_completed_emits_followup(tmp_path: Path, capsys) -> None:
    ok = tmp_path / "ok.sh"
    ok.write_text("#!/bin/sh\nexit 0\n")
    ok.chmod(0o755)
    (tmp_path / "capabilities.yaml").write_text(
        _manifest(
            f"  - id: cheap-a\n    description: d\n    given: g\n    when: w\n    then: t\n"
            f"    tier: cheap\n    deps: []\n    check:\n      shell: {ok}\n",
        )
    )
    payload = json.dumps({
        "hook_event_name": "stop",
        "status": "completed",
        "loop_count": 0,
        "workspace_roots": [str(tmp_path)],
    })
    rc = main(["gate"])  # stdin: see Step 3 — tests should pass payload via monkeypatch
```

Do **not** leave the `main(["gate"])` stub incomplete. Implement the test as:

```python
def test_cmd_gate_cursor_completed_emits_followup(tmp_path: Path, monkeypatch, capsys) -> None:
    ...
    monkeypatch.setattr(
        "caps.cli.sys.stdin",
        type("S", (), {"read": lambda self: payload})(),
    )
    from caps.cli import cmd_gate
    rc = cmd_gate(payload, datetime.now(UTC))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "followup_message" in out
    assert "cheap-a" in out["followup_message"]


def test_cmd_gate_cursor_aborted_emits_empty(tmp_path: Path, capsys) -> None:
    (tmp_path / "capabilities.yaml").write_text("capabilities:\n")
    from caps.cli import cmd_gate
    payload = json.dumps({
        "hook_event_name": "stop",
        "status": "aborted",
        "loop_count": 0,
        "workspace_roots": [str(tmp_path)],
    })
    rc = cmd_gate(payload, datetime.now(UTC))
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_cmd_gate_cursor_loop_limit_emits_empty(tmp_path: Path, capsys) -> None:
    ok = tmp_path / "ok.sh"
    ok.write_text("#!/bin/sh\nexit 0\n")
    ok.chmod(0o755)
    (tmp_path / "capabilities.yaml").write_text(
        _manifest(
            f"  - id: cheap-a\n    description: d\n    given: g\n    when: w\n    then: t\n"
            f"    tier: cheap\n    deps: []\n    check:\n      shell: {ok}\n",
        )
    )
    from caps.cli import cmd_gate
    payload = json.dumps({
        "hook_event_name": "stop",
        "status": "completed",
        "loop_count": 5,
        "workspace_roots": [str(tmp_path)],
    })
    rc = cmd_gate(payload, datetime.now(UTC))
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {}
```

Add `test_doctor_defaults_to_project_settings(tmp_path)` that writes `tmp_path / ".claude" / "settings.json"` with `_caps: caps-stop-gate` and asserts `diagnose(tmp_path, now)` reports that path, not `~/.claude/settings.json`.

- [ ] **Step 2: Run to verify fail**

Run: `cd python && uv run --frozen pytest tests/test_caps_kit.py -q --tb=line`

Expected: FAIL on `resolve_root` / `cmd_gate` / doctor default.

- [ ] **Step 3: Implement**

`resolve_root`:

```python
def resolve_root(payload: dict) -> Path | None:
    for raw in payload.get("workspace_roots") or []:
        r = find_root(Path(raw))
        if r:
            return r
    env = os.environ.get("CURSOR_PROJECT_DIR")
    if env:
        r = find_root(Path(env))
        if r:
            return r
    cwd = payload.get("cwd")
    ...
```

Add `import os` to `caps/gate.py`.

`cmd_gate`: if `payload.get("status") in {"aborted", "error"}` or `int(payload.get("loop_count") or 0) >= 5`, print `{}` and return 0. If blocking and (`hook_event_name == "stop"` or `"workspace_roots" in payload`), print `{"followup_message": decision.reason}`. Else keep Claude `decision: block`. Always return 0.

`diagnose`: if `settings_path is None`, use `root / ".claude" / "settings.json"` when it exists. Also inspect `root / ".cursor" / "hooks.json"` for `hooks.stop` with a command containing `caps`.

Create `.cursor/hooks/caps-stop-gate.sh` that `cd`s to repo root, finds `capabilities.yaml`, runs `PYTHONPATH=<repo> python3 -m caps gate` (prefer `python/.venv/bin/python` if executable), always `exit 0`.

Create `.cursor/hooks.json`:

```json
{
  "version": 1,
  "hooks": {
    "stop": [
      {
        "command": ".cursor/hooks/caps-stop-gate.sh",
        "timeout": 30,
        "loop_limit": 5
      }
    ]
  }
}
```

Do not add `beforeShellExecution`. Do not set `failClosed: true`.

- [ ] **Step 4: Re-run tests**

Run: `cd python && uv run --frozen pytest tests/test_caps_kit.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add caps/gate.py caps/cli.py caps/doctor.py python/tests/test_caps_kit.py .cursor/hooks.json .cursor/hooks/caps-stop-gate.sh
git commit -m "feat(caps): Cursor stop adapter and project-local doctor"
```

---

### Task 3: SQL cancel + MLflow eval production path

**Files:**
- Modify: `python/src/apx_agent/_sql.py` (`run_sql`)
- Modify: `python/tests/test_sql.py` (`TestRunSql`)
- Create: `python/tests/test_dev_ui_eval_mlflow_reality_ctk.py`
- Test: those two test files

**Interfaces:**
- Consumes: `run_sql(ws, sql, ...)`, `ws.statement_execution.execute_statement` / `cancel_execution`, `_fetch_eval_cases_sync`, `build_dev_ui_router`, `GET /_apx/eval/data`
- Produces: `run_sql(..., cancel_token: CancelToken | None = None)` calls `cancel_execution(statement_id)` when the token is cancelled during poll; eval GET with `MLFLOW_EXPERIMENT_ID` returns MLflow-mapped cases and ignores a conflicting `evals.json`

- [ ] **Step 1: Write failing SQL cancel test**

In `python/tests/test_sql.py` class `TestRunSql`:

```python
    def test_cancel_token_cancels_running_statement(self):
        from databricks.sdk.service.sql import StatementState
        from apx_agent._cancellation import CancelToken

        pending = MagicMock()
        pending.status.state = StatementState.RUNNING
        pending.statement_id = "stmt-cancel"
        ws = self._make_ws(pending)
        ws.statement_execution.get_statement.return_value = pending
        token = CancelToken()

        def _cancel_after_poll(*_a, **_k):
            token.cancel("test")
            return pending

        ws.statement_execution.get_statement.side_effect = _cancel_after_poll
        with pytest.raises(Exception):
            run_sql(
                ws, "SELECT 1", warehouse_id="wh-1",
                poll_interval_s=0.01, poll_timeout_s=1,
                cancel_token=token,
            )
        ws.statement_execution.cancel_execution.assert_called_with("stmt-cancel")
```

- [ ] **Step 2: Run SQL test — expect fail**

Run: `cd python && uv run --frozen pytest tests/test_sql.py::TestRunSql::test_cancel_token_cancels_running_statement -q`

Expected: FAIL (`cancel_token` unexpected / `cancel_execution` not called)

- [ ] **Step 3: Implement cancel wiring**

In `run_sql`, add `cancel_token=None`. After `execute_statement`, if `cancel_token` is cancelled, call `ws.statement_execution.cancel_execution(result.statement_id)` and raise. Inside `_await_statement_completion` loop (or the poll loop `run_sql` uses), check the token each iteration and cancel the statement id.

Keep `wait_timeout="30s"` unchanged. Do not treat RUNNING as empty success.

- [ ] **Step 4: Write failing MLflow eval test**

Create `python/tests/test_dev_ui_eval_mlflow_reality_ctk.py` modeled on `TestTracesListReadAfterWrite`: local `file://` MLflow, `MLFLOW_EXPERIMENT_ID` set, write a decoy `evals.json` with `"question": "FROM_FILE"`, log a real span whose assessments include a **dict** `{"assessment_name": "quality", "feedback": {"value": True}, "rationale": "grounded"}` (or the shape `_fetch_eval_cases_sync` actually reads). `GET /_apx/eval/data` must return a case whose question is **not** `FROM_FILE` and whose `trace_id` matches the logged trace.

If assessments cannot be attached through the file store in one step, call `_fetch_eval_cases_sync` with a monkeypatched `mlflow.search_traces` returning a one-row DataFrame of dict assessments, then also hit the route with `MLFLOW_EXPERIMENT_ID` set and `_EVAL_CASES_CACHE` cleared. The route test must fail if GET reads `evals.json` while the env var is set.

- [ ] **Step 5: Run eval test — expect fail or already fail on cache/shape**

Run: `cd python && uv run --frozen pytest tests/test_dev_ui_eval_mlflow_reality_ctk.py -q --tb=short`

Expected: FAIL until the route + parser match the test contract.

- [ ] **Step 6: Fix production path only as needed**

If GET already prefers MLflow when `MLFLOW_EXPERIMENT_ID` is set, only fix assessment dict parsing / cache isolation in the test. If GET still returns decoy file contents after a successful MLflow fetch of `[]` (swallowed exception), that is a product bug: empty MLflow result must not be indistinguishable from "use the file" when the experiment id is set. Spec: the cap **fails** if implementation falls back to `evals.json` while an experiment is configured. Change `_dev.py` `eval_data_get` so that when `experiment_id` is set, return the MLflow list (possibly empty) and **do not** read `evals.json`.

- [ ] **Step 7: Re-run both tests**

Run: `cd python && uv run --frozen pytest tests/test_sql.py::TestRunSql::test_cancel_token_cancels_running_statement tests/test_dev_ui_eval_mlflow_reality_ctk.py -q`

Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add python/src/apx_agent/_sql.py python/src/apx_agent/_dev.py python/tests/test_sql.py python/tests/test_dev_ui_eval_mlflow_reality_ctk.py
git commit -m "test: prove SQL cancel and MLflow eval GET path"
```

Only include `_dev.py` if Step 6 required it.

---

### Task 4: Add the 12 cheap capabilities

**Files:**
- Modify: `capabilities.yaml` via `caps add` only
- Create: failing check stubs from `caps add`, then replace with the shell commands below
- Test: `python -m caps doctor --json` and `python -m caps verify --tier cheap`

**Interfaces:**
- Consumes: `python -m caps add --id ... --tier cheap --shell '...'`
- Produces: twelve never-proven then proven cheap entries with narrow `deps`

From repo root (`PYTHONPATH=.`), run `caps add` once per id. Use `--shell` with `cd python && uv run --frozen pytest ... -q`. After add, `caps add` scaffolds a failing stub if `--check` is used; with `--shell` it records the shell string only.

Exact commands (run sequentially; stop on non-zero):

```bash
export PYTHONPATH=.
python3 -m caps add --id apps-scaffold-host-wiring --tier cheap \
  --description "Apps scaffold/generate host artifacts are wired and non-empty" \
  --given "an Apps-target agent declaration" \
  --when "scaffold or generate writes the project" \
  --then "host artifacts import the declared agent and expose the serving bridge" \
  --deps python/src/apx_agent/cli.py --deps python/src/apx_agent/_project_gen.py \
  --deps python/src/apx_agent/_appkit_host_generator.py \
  --shell 'cd python && uv run --frozen pytest tests/test_scaffold_reality_ctk.py::test_scaffold_outputs_are_real_not_just_present tests/test_project_gen_reality_ctk.py::test_generated_project_files_are_real_not_just_present tests/test_appkit_host_generator.py::test_writes_generated_appkit_host_skeleton -q'

python3 -m caps add --id served-runtime-readiness --tier cheap \
  --description "create_app mounts serving routes and /readyz degrades without 500" \
  --given "a declared LlmAgent" \
  --when "create_app starts" \
  --then "/invocations /responses A2A discovery and /readyz are mounted; degraded is 503 not 500" \
  --deps python/src/apx_agent/_wiring.py --deps python/src/apx_agent/_readyz.py \
  --shell 'cd python && uv run --frozen pytest tests/test_readyz.py::test_create_app_serves_readyz tests/test_readyz.py::test_readyz_ready_when_llm_and_trace_ok tests/test_readyz.py::test_readyz_never_500s tests/test_readyz.py::test_readyz_degraded_when_llm_empty -q'

python3 -m caps add --id apps-identity-and-user-scoping --tier cheap \
  --description "Apps fail closed without caller identity; principals cannot share session keys" \
  --given "an Apps request" \
  --when "identity is missing or two principals share a raw session id" \
  --then "governed ops reject; session and memory keys stay isolated" \
  --deps python/src/apx_agent/_obo.py --deps python/src/apx_agent/_a2a.py \
  --shell 'cd python && uv run --frozen pytest tests/test_obo.py::test_no_obo_in_app_rejects_by_default tests/test_obo.py::test_scope_session_key_namespaces_by_principal tests/test_a2a.py::TestA2AAppsIdentityGate::test_apps_without_identity_returns_401 -q'

python3 -m caps add --id a2a-delegation-and-identity --tier cheap \
  --description "Declared sub_agents execute and forward the caller token" \
  --given "sub_agents=[url] across two hops" \
  --when "the root delegates" \
  --then "the peer runs and the leaf sees the original caller token" \
  --deps python/src/apx_agent/_remote.py --deps python/src/apx_agent/_agent_tool.py \
  --shell 'cd python && uv run --frozen pytest tests/test_cross_agent_delegation_reality_ctk.py::test_config_declared_sub_agent_really_executes tests/test_multi_hop_identity_reality_ctk.py::test_obo_token_reaches_leaf_through_two_hops -q'

python3 -m caps add --id governed-tool-approval --tier cheap \
  --description "Served approval/deny and watchdog fail-closed" \
  --given "an approval-required or watchdog-gated tool" \
  --when "the tool is invoked on the served path" \
  --then "deny blocks; approve resume runs; transport errors fail closed" \
  --deps python/src/apx_agent/_policy.py --deps python/src/apx_agent/_watchdog.py \
  --shell 'cd python && uv run --frozen pytest tests/test_approval_served_reality_ctk.py::test_predict_surfaces_approval_then_resume_approve_runs_tool tests/test_watchdog_fail_closed_reality_ctk.py::test_transport_error_fails_closed_by_default -q'

python3 -m caps add --id sql-terminal-state-and-cancellation --tier cheap \
  --description "SQL polls to a terminal state and cancellation reaches the statement" \
  --given "execute_statement returns RUNNING" \
  --when "run_sql polls or a cancel token fires" \
  --then "success returns rows; failure is real; timeout is distinct; cancel_execution is called" \
  --deps python/src/apx_agent/_sql.py \
  --shell 'cd python && uv run --frozen pytest tests/test_sql.py::TestRunSql::test_polls_to_completion_when_still_running_after_wait_timeout tests/test_sql.py::TestRunSql::test_raises_clear_timeout_message_when_still_running_after_poll_budget tests/test_sql.py::TestRunSql::test_genuine_failure_after_polling_still_reports_real_error tests/test_sql.py::TestRunSql::test_cancel_token_cancels_running_statement -q'

python3 -m caps add --id mlflow-trace-and-eval-read --tier cheap \
  --description "MLflow traces and eval cases use the production read path" \
  --given "a local MLflow experiment with a trace and quality assessment" \
  --when "trace search and eval GET run" \
  --then "the trace and eval case are returned; evals.json is not used when MLFLOW_EXPERIMENT_ID is set" \
  --deps python/src/apx_agent/_mlflow_tracing.py --deps python/src/apx_agent/_dev.py \
  --shell 'cd python && uv run --frozen pytest tests/test_trace_search_reality_ctk.py::test_search_traces_for_experiment_reads_a_real_trace tests/test_dev_ui_eval_mlflow_reality_ctk.py -q'

python3 -m caps add --id trace-feedback-identity-roundtrip --tier cheap \
  --description "Feedback writes with trusted identity and reads back" \
  --given "a deployed-style request with OBO identity" \
  --when "POST then GET /_apx/feedback" \
  --then "missing identity is 401; source email round-trips" \
  --deps python/src/apx_agent/_trace_feedback_api.py \
  --shell 'cd python && uv run --frozen pytest tests/test_trace_feedback_api.py::test_deployed_feedback_requires_obo_and_human_identity tests/test_trace_feedback_api.py::test_deployed_feedback_uses_forwarded_email_as_source -q'

python3 -m caps add --id durable-session-and-lakebase-wiring --tier cheap \
  --description "Checkpoint recall and Lakebase pool hygiene" \
  --given "declared durable session or lakebase memory" \
  --when "the app starts, serves two turns, or shuts down" \
  --then "turn 2 recalls turn 1; PostgresSaver is selected; setup failure closes the pool; engines dispose" \
  --deps python/src/apx_agent/_chat_agent.py --deps python/src/apx_agent/_checkpoint_lakebase.py \
  --shell 'cd python && uv run --frozen pytest tests/test_short_term_memory_served_reality_ctk.py::test_served_predict_recalls_prior_turn_via_checkpointer tests/test_checkpoint_lakebase_reality_ctk.py::test_resolve_checkpointer_builds_postgres_saver_for_lakebase tests/test_checkpointer_pool_setup_leak_reality_ctk.py::test_setup_failure_closes_the_pool tests/test_lakebase_engine_dispose_reality_ctk.py::test_conversation_store_engine_disposed_on_lifespan_shutdown -q'

python3 -m caps add --id serving-protocol-and-concurrency --tier cheap \
  --description "Chat/Responses streams complete and concurrent turns do not block" \
  --given "compiled ChatAgent and ResponsesAgent" \
  --when "streaming and overlapping requests arrive" \
  --then "protocol events complete in order and two turns proceed concurrently" \
  --deps python/src/apx_agent/_chat_agent.py --deps python/src/apx_agent/_responses_agent.py --deps python/src/apx_agent/_async_bridge.py \
  --shell 'cd python && uv run --frozen pytest tests/test_responses_agent.py::TestStream::test_yields_response_output_item_done_then_completed tests/test_invocations_route.py::TestStreamingProtocol::test_returns_sse_with_chunk_per_event tests/test_serving_concurrency_reality_ctk.py::test_invocations_nonstream_serves_two_turns_concurrently -q'

python3 -m caps add --id observability-trace-roundtrip --tier cheap \
  --description "Logged traces appear in the dev UI list" \
  --given "an instrumented agent run" \
  --when "GET /_apx/traces" \
  --then "the logged trace_id is listed" \
  --deps python/src/apx_agent/_dev.py --deps python/src/apx_agent/_trace_store.py \
  --shell 'cd python && uv run --frozen pytest tests/test_dev_ui_reality_ctk.py::TestTracesListReadAfterWrite::test_logged_trace_appears_in_list_route tests/test_cross_agent_delegation_reality_ctk.py::test_cross_agent_traces_join_on_one_tag -q'

python3 -m caps add --id deploy-artifact-and-gate-freshness --tier cheap \
  --description "Deploy aborts on stale pin or degraded readyz" \
  --given "a deployable Apps project" \
  --when "agents deploy runs" \
  --then "pin mismatch and degraded readyz abort before success is claimed" \
  --deps python/src/apx_agent/cli.py \
  --shell 'cd python && uv run --frozen pytest tests/test_deploy_apps.py::test_pin_mismatch_aborts_deploy tests/test_deploy_apps.py::test_readyz_gate_fails_deploy_when_degraded -q'
```

If a pytest node path is wrong (class name), collect with `cd python && uv run pytest --collect-only -q tests/<file>.py | rg test_name` and fix the `--shell` by `caps` re-add is not supported — edit only via deleting the entry is forbidden; use a follow-up `caps` workflow: if `add` already wrote the id, change the shell by implementing a tiny helper **only if** `caps` has no edit command — then the smallest path is to load YAML after add and the skill says never hand-edit. If add succeeded with a bad node, `python3 -m caps` has no `edit`. Fix by replacing the shell string is a manifest_edit. Add `caps/manifest_edit.py` only if needed; otherwise delete the one bad id by restoring capabilities.yaml from git and re-running **all** adds in a clean file. Practical rule: run `caps add` against a **copy** first or verify collect-only before add.

Before each add, run collect-only:

```bash
cd python && uv run --frozen pytest --collect-only -q tests/test_readyz.py::test_create_app_serves_readyz
```

Expected: collected 1 item.

If a collected node id differs from the `--shell` string, restore `capabilities.yaml` from git and re-run all `caps add` commands rather than hand-editing the manifest.

- [ ] **Step 2: Verify cheap caps**

```bash
PYTHONPATH=. python3 -m caps doctor --json
PYTHONPATH=. python3 -m caps verify --tier cheap
PYTHONPATH=. python3 -m caps status --check --tier cheap
```

Expected: doctor ok/warn only (no FAIL); verify exit 0; status --check exit 0.

- [ ] **Step 3: Gitignore ledger**

Add to `.gitignore`:

```
.ctk/ledger.json
```

- [ ] **Step 4: Commit**

```bash
git add capabilities.yaml .gitignore
git commit -m "feat(caps): declare twelve cheap runtime promises"
```

Do not add `.ctk/ledger.json`.

---

### Task 5: PR CI cheap verify

**Files:**
- Modify: `Makefile` (`check` target)
- Modify: `.github/workflows/ci.yml` (`test` job)
- Modify: `CLAUDE.md`, `AGENTS.md` (short paragraph)

**Interfaces:**
- Consumes: `python -m caps verify --tier cheap` from repo root with `PYTHONPATH=.`
- Produces: `make check` runs cheap caps after pytest; CI `test` job does the same when `code=true`; live caps are not invoked

- [ ] **Step 1: Makefile**

After the existing pytest line:

```make
	PYTHONPATH=. python3 -m caps verify --tier cheap
```

CI Python may be uv's. Prefer:

```make
	cd python && uv run --frozen python -c "import sys; sys.path.insert(0,'..'); from caps.cli import main; raise SystemExit(main(['verify','--tier','cheap']))"
```

Use that form so PyYAML comes from the uv env.

- [ ] **Step 2: ci.yml**

After `uv run pytest tests/ -n auto --tb=short`, add a step `Verify cheap caps` with `working-directory: python` and the same `uv run --frozen python -c ...` command. Gate with `if: needs.changes.outputs.code == 'true'`. Docs-only path stays skip.

- [ ] **Step 3: Docs**

In `CLAUDE.md` and `AGENTS.md` Commands section, add:

```
python -m caps verify --tier cheap   # framework promises (CI)
python -m caps verify --tier live    # requires APX_CAPS_PROFILE; not PR CI
```

State that skipped live tests are errors, not passes.

- [ ] **Step 4: Run cheap verify locally**

Run the Makefile snippet once.

Expected: exit 0

- [ ] **Step 5: Commit**

```bash
git add Makefile .github/workflows/ci.yml CLAUDE.md AGENTS.md
git commit -m "ci: require cheap capability proofs on code PRs"
```

---

### Task 6: Seven live probes (no workspace selection)

**Files:**
- Create: `checks/_live.py`
- Create: `checks/prove_app_boot_readyz.py`
- Create: `checks/prove_user_isolation_a2a.py`
- Create: `checks/prove_sql_cold_terminal.py`
- Create: `checks/prove_mlflow_eval_feedback.py`
- Create: `checks/prove_lakebase_restart.py`
- Create: `checks/prove_appkit_runtime_parity.py`
- Create: `checks/prove_grounded_platform_tools.py`
- Create: `python/tests/test_live_caps_config.py` — missing profile exits 3; ambient profile rejected
- Modify: `capabilities.yaml` via `caps add --tier live`
- Create: `.github/workflows/caps-live.yml`

**Interfaces:**
- Consumes: `APX_CAPS_PROFILE`, `APX_CAPS_APP_URL`, optional resource env vars listed in each script's module docstring
- Produces: `require_profile() -> str` exits 3 if unset; rejects using `DATABRICKS_CONFIG_PROFILE` as substitute; `ERROR_EXIT = 3`; live workflow `workflow_dispatch` + weekly cron, `if: github.event_name != 'pull_request'` equivalent by not listing `pull_request`

- [ ] **Step 1: Write config tests**

```python
# python/tests/test_live_caps_config.py
import os
import runpy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def test_missing_profile_exits_3(monkeypatch):
    monkeypatch.delenv("APX_CAPS_PROFILE", raising=False)
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "should-not-count")
    sys.path.insert(0, str(REPO / "checks"))
    import importlib
    if " _live" in sys.modules:
        del sys.modules[" _live"]
    import importlib.util
    spec = importlib.util.spec_from_file_location("_live", REPO / "checks" / "_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(SystemExit) as e:
        mod.require_profile()
    assert e.value.code == 3
```

- [ ] **Step 2: Run — expect fail (module missing)**

Run: `cd python && uv run --frozen pytest tests/test_live_caps_config.py -q`

Expected: FAIL import

- [ ] **Step 3: Implement `_live.py`**

```python
from __future__ import annotations

import os
import re
import sys

ERROR_EXIT = 3


def require_profile() -> str:
    profile = os.environ.get("APX_CAPS_PROFILE")
    if not profile:
        print("live cap: set APX_CAPS_PROFILE (ambient DATABRICKS_CONFIG_PROFILE is not accepted)", file=sys.stderr)
        raise SystemExit(ERROR_EXIT)
    return profile


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"live cap: missing {name}", file=sys.stderr)
        raise SystemExit(ERROR_EXIT)
    return val


def redact(text: str) -> str:
    return re.sub(r"(Bearer |dapi)[^\s]+", r"\1[redacted]", text)
```

Each `prove_*.py` starts with `require_profile()`, prints a one-line plan, and if remaining resource env vars are missing, `raise SystemExit(3)`. Do not call Databricks until required vars exist. Do not read `DATABRICKS_CONFIG_PROFILE` as the proof profile.

Example `checks/prove_app_boot_readyz.py`:

```python
from _live import require_env, require_profile, ERROR_EXIT, redact
import sys
from databricks.sdk import WorkspaceClient
import httpx

def main() -> int:
    profile = require_profile()
    url = require_env("APX_CAPS_APP_URL").rstrip("/")
    ws = WorkspaceClient(profile=profile)
    token = ws.config.token
    if not token:
        print("live cap: profile produced no token", file=sys.stderr)
        return ERROR_EXIT
    r = httpx.get(f"{url}/readyz", headers={"Authorization": f"Bearer {token}"}, timeout=60.0)
    if r.status_code != 200:
        print(redact(f"readyz HTTP {r.status_code}: {r.text[:300]}"), file=sys.stderr)
        return 1
    if r.json().get("status") != "ready":
        print(redact(r.text[:300]), file=sys.stderr)
        return 1
    print("readyz ready")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

Other prove scripts follow the same pattern: isolation uses two user tokens from `APX_CAPS_USER_A_TOKEN` / `APX_CAPS_USER_B_TOKEN` (never commit tokens); SQL uses `APX_CAPS_WAREHOUSE_ID`; MLflow uses `APX_CAPS_EXPERIMENT_ID`; Lakebase uses `APX_CAPS_LAKEBASE_DSN` or documented workspace resources; AppKit uses `APX_CAPS_APP_URL` plus SHA env `APX_CAPS_EXPECTED_SHA`; Genie/KA use `APX_GENIE_SPACE_ID` / `APX_KA_ENDPOINT_NAME` already referenced by existing live tests — still require `APX_CAPS_PROFILE`.

Until resources exist, scripts exit 3 after `require_env`. That is **error**, not pass. Do not `pytest.skip`.

- [ ] **Step 4: Config test PASS; missing-env probe exits 3**

```bash
cd python && uv run --frozen pytest tests/test_live_caps_config.py -q
env -u APX_CAPS_PROFILE PYTHONPATH=checks python3 checks/prove_app_boot_readyz.py; echo $?
```

Expected: pytest PASS; script exit 3.

- [ ] **Step 5: caps add live entries**

```bash
PYTHONPATH=. python3 -m caps add --id live-app-boot-readyz --tier live \
  --description "Deployed App authenticated /readyz is ready" \
  --given "APX_CAPS_PROFILE and APX_CAPS_APP_URL" \
  --when "GET /readyz" \
  --then "HTTP 200 and status=ready" \
  --deps checks/prove_app_boot_readyz.py --deps python/src/apx_agent/_readyz.py \
  --shell 'python3 checks/prove_app_boot_readyz.py'
```

Repeat for:

- `live-app-user-isolation-a2a` → `checks/prove_user_isolation_a2a.py`
- `live-sql-cold-terminal-state` → `checks/prove_sql_cold_terminal.py`
- `live-mlflow-eval-feedback-roundtrip` → `checks/prove_mlflow_eval_feedback.py`
- `live-lakebase-restart-durability` → `checks/prove_lakebase_restart.py`
- `live-appkit-runtime-parity` → `checks/prove_appkit_runtime_parity.py`
- `live-grounded-platform-tools-obo` → `checks/prove_grounded_platform_tools.py`

- [ ] **Step 6: Assert PR CI will not run live**

`PYTHONPATH=. python3 -m caps verify --tier cheap` must not execute `checks/prove_*.py`.

`PYTHONPATH=. python3 -m caps verify --tier live` without env must exit 1 with live caps in `error` (not pass).

- [ ] **Step 7: Workflow**

`.github/workflows/caps-live.yml`:

```yaml
name: Caps live
on:
  workflow_dispatch:
  schedule:
    - cron: "0 14 * * 1"
jobs:
  live:
    runs-on: ubuntu-latest
    if: github.event_name != 'pull_request'
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v8.1.0
      - run: uv python install 3.13
        working-directory: python
      - run: uv sync --group dev --all-extras
        working-directory: python
      - name: Verify live caps
        env:
          APX_CAPS_PROFILE: ${{ vars.APX_CAPS_PROFILE }}
          APX_CAPS_APP_URL: ${{ vars.APX_CAPS_APP_URL }}
        run: |
          cd python && uv run --frozen python -c "import sys; sys.path.insert(0,'..'); from caps.cli import main; raise SystemExit(main(['verify','--tier','live']))"
```

Do not put a profile name in the YAML. If `vars.APX_CAPS_PROFILE` is empty, probes exit 3 and the job fails visibly — operators set repo variables. Do not add this workflow to `pull_request`.

- [ ] **Step 8: Commit**

```bash
git add checks python/tests/test_live_caps_config.py capabilities.yaml .github/workflows/caps-live.yml
git commit -m "feat(caps): declare live Databricks proofs with explicit profile"
```

Do not run live verify against a real workspace in this task.

---

### Task 7: Full gate read-back

**Files:** none new unless doctor/CI docs need a one-line fix

- [ ] **Step 1: Caps doctor and cheap verify**

```bash
cd python && uv run --frozen pytest tests/test_caps_kit.py tests/test_live_caps_config.py tests/test_dev_ui_eval_mlflow_reality_ctk.py tests/test_sql.py::TestRunSql::test_cancel_token_cancels_running_statement -q
PYTHONPATH=.. uv run --frozen python -c "import sys; sys.path.insert(0,'..'); from caps.cli import main; raise SystemExit(main(['doctor']))"
PYTHONPATH=.. uv run --frozen python -c "import sys; sys.path.insert(0,'..'); from caps.cli import main; raise SystemExit(main(['verify','--tier','cheap']))"
PYTHONPATH=.. uv run --frozen python -c "import sys; sys.path.insert(0,'..'); from caps.cli import main; raise SystemExit(main(['status','--check','--tier','cheap']))"
```

Expected: all exit 0. Count capabilities: 12 cheap + 7 live in `capabilities.yaml`.

- [ ] **Step 2: `make check`**

Run: `make check`

Expected: pytest green and cheap caps proven. Do not include unrelated dirty files.

- [ ] **Step 3: Confirm live not in PR file**

`rg pull_request .github/workflows/caps-live.yml` must match nothing.

`rg 'verify --tier live' .github/workflows/ci.yml` must match nothing.

- [ ] **Step 4: Final commit only if Step 1–3 required doc/CI fixes**

Otherwise stop. No empty commit.

---

## Spec coverage

| Spec requirement | Task |
|---|---|
| Caps framework tests (manifest, skip, stale, waiver, gate, python path, tier) | 1–2 |
| Cheap-only local block; live release/CI split | 1, 5, 6 |
| Cursor version 1, workspace_roots, followup_message, loop_limit, no hard veto | 2 |
| 12 cheap + 7 live in capabilities.yaml | 4, 6 |
| MLflow eval not evals.json when experiment set | 3 |
| SQL RUNNING not empty success; cancel forwarded | 3 |
| `caps verify --tier cheap` in CI | 5 |
| Live never on PR; explicit `APX_CAPS_PROFILE` | 6 |
| doctor validates entries and project hooks | 2, 7 |
| No unrelated dirty files / secrets | Global + 7 |
| Readiness domains mapped; a11y/LLM cost excluded | Inventory in spec; no extra caps |

## Placeholder scan

No TBD/TODO. Pytest class names in Task 4 must be confirmed with `--collect-only` before `caps add`.
