# Remove the Model Serving deploy target — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** apx-agent deploys agents to Databricks Apps only. Every path that hosted an agent on Model Serving is removed, and anyone still asking for it gets an error that names `--target apps`.

**Architecture:** Three PRs, merged in order, each cut from the then-current `main` and never stacked.
- **PR A (Tasks 1–5)** cuts the CLI surface. The serving code becomes unreachable.
- **PR B (Tasks 6–8)** deletes that dead code and fixes stale docstrings in shared code.
- **PR C (Tasks 9–10)** rewrites the docs and examples.

**Tech Stack:** Python ≥3.10, click, pytest, ctk.

**Spec:** `docs/design/remove-model-serving.md`.

## Global Constraints

- **Kept by the spec — do not delete or change behavior:**
  - `compile_to_responses_agent` / `ApxResponsesAgent` (the `responses_agent` runtime);
  - `_apps_registry` / `set_uc_tags_for_agent` / `_resolve_project_uc_name` (Apps UC registration);
  - LLM and embedding invocation (`model=`, `/serving-endpoints/{model}/invocations`, `_embeddings.py`), and Vector Search;
  - `_external_model.py`;
  - `eval --endpoint-url` / `endpoint_predict_fn`;
  - `_hot_swap_apps.py` / `_canary_apps.py`;
  - the `APX_AGENT_MODEL_OVERRIDE` runtime read;
  - `supervisor add --app`; and `advertise` / `publish` with `--endpoint-type` (both values).
- **The error text**, exactly: `Model Serving was removed: apx deploys agents to Databricks Apps. Run apx-agent <command> --target apps.`
- **Worktrees.**
  - PR A uses this worktree (`apx-remove-model-serving`, branch `stuagano/apx-remove-model-serving`).
  - PRs B and C each get a new Orca worktree from the then-current `origin/main`, created after the previous PR merges.
  - Carry the ledger (`.superpowers/sdd/<plan>/progress.md`) between worktrees through a file outside the worktree.
- **Gate (every PR):**
  - `make check`;
  - `pre-commit run --files <changed>`;
  - `uv run pyright src/apx_agent/`;
  - the PR CI subset under xdist, twice. In zsh: ``paths=$(BASE=origin/main HEAD=HEAD EVENT_NAME=pull_request PR_PYTEST_CHANGED_FILES="$(git diff --name-only origin/main...HEAD)" bash ../scripts/pr-pytest-paths.sh); uv run pytest $(echo $paths) --numprocesses auto --timeout=90 --timeout-method=thread``;
  - **and** a full suite run without credentials: ``env $(env | grep -o '^DATABRICKS_[A-Z_]*' | sed 's/^/-u /') HOME=/tmp/nohome uv run pytest --numprocesses auto``. CI has no Databricks credentials.
- **Lint bans:**
  - `.get(k, "")` and `x or ""`;
  - `getattr(x, "a", "<str>")`;
  - `object` annotations;
  - `str = ""` defaults;
  - invented env defaults;
  - `tuple[...]` returns;
  - unconditional `@pytest.mark.skip`.
- **Commits:**
  - end with `Co-authored-by: Isaac <no-reply@databricks.com>`;
  - run `git commit --quiet --message …` **alone** in its command. The hook misreads `-n`/`--numprocesses` elsewhere in the same command line.
  - Never `git stash`.
  - Don't touch `uv.lock`.
- **Ledger every deleted test by name.** Delete only tests that pin removed behavior.
- **Public repo:** no roadmap in commits or PRs.

## Review Focus

1. **A required option before `--target`.** For example `canary deploy --target model-serving` without its other required options. Expected: the removal message, not "Missing option". Pinned in Task 1, with the `--target` callback.
2. **A project with no target flag that used to auto-detect Model Serving** (the old flat `agent.py` + `app.py` layout with no `databricks.yml`). Expected: `deploy` resolves to Apps and proceeds or fails with an Apps-shaped error, never a serving deploy. Pinned in Task 3.
3. **A script passing a removed `deploy` flag**, such as `--workload-size`. Expected: click's "No such option" error, exit code 2, and no side effect. Pinned in Task 3.
4. **`APX_AGENT_MODEL_OVERRIDE` at runtime after PR B.** Expected: still honored by the responses and chat runtimes. Pinned in Task 7.
5. **`advertise --endpoint-type model-serving`** (registering an existing serving endpoint as a peer). Expected: unchanged; the spec keeps it. Pinned in Task 4.

---

## PR A — the user-facing cut

### Task 1: `--target model-serving` is an error on every command that took it

**Files:**
- Modify: `python/src/apx_agent/cli.py`. The `--target` options are on:
  - `agents scaffold` (~4537);
  - `agents deploy` (~6952);
  - `agents hot-swap` (~13546);
  - canary `status` (~16157), `deploy` (~16269), `promote` (~16462), `rollback` (~16625) and `analyze` (~16764).
- Test: `python/tests/test_model_serving_removed.py` (create)

**Interfaces:**
- Produces:
  - `_MODEL_SERVING_REMOVED: str`, a format string with `{command}`;
  - `_reject_model_serving(command: str) -> Callable[[click.Context, click.Parameter, str | None], str | None]`, a click option callback.

- [ ] **Step 1: Write the failing test**

```python
"""Model Serving is removed: --target model-serving names Apps (sub-project 4)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from apx_agent import cli

_COMMANDS = [
    ("agents deploy", ["agents", "deploy", "--target", "model-serving"]),
    ("agents scaffold", ["agents", "scaffold", "x", "--target", "model-serving", "--no-interactive"]),
    ("agents hot-swap", ["agents", "hot-swap", "--target", "model-serving"]),
    ("canary status", ["canary", "status", "--target", "model-serving"]),
    ("canary deploy", ["canary", "deploy", "--target", "model-serving"]),
    ("canary promote", ["canary", "promote", "--target", "model-serving"]),
    ("canary rollback", ["canary", "rollback", "--target", "model-serving"]),
    ("canary analyze", ["canary", "analyze", "--target", "model-serving"]),
]


@pytest.mark.parametrize(("command", "args"), _COMMANDS, ids=[c for c, _ in _COMMANDS])
def test_model_serving_target_names_apps(command: str, args: list[str], tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    touched = MagicMock(side_effect=AssertionError("must fail before any workspace access"))
    for name in ("_make_scaffold_workspace_client", "_make_ws_for_scaffold", "_run_databricks_cmd"):
        monkeypatch.setattr(cli, name, touched, raising=False)
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 2, result.output  # a usage error
    assert f"Model Serving was removed: apx deploys agents to Databricks Apps. Run apx-agent {command} --target apps." in result.output
    touched.assert_not_called()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_model_serving_removed.py -q -p no:cacheprovider`
Expected: all 8 FAIL. The commands either run their serving path or fail with "Missing option".

- [ ] **Step 3: Implement.** Near the other module-level CLI helpers in `cli.py` (e.g. above `_ApxGroup`):

```python
_MODEL_SERVING_REMOVED = (
    "Model Serving was removed: apx deploys agents to Databricks Apps. Run apx-agent {command} --target apps."
)


def _reject_model_serving(command: str) -> Callable[[click.Context, click.Parameter, str | None], str | None]:
    """A --target callback: 'model-serving' is accepted for one release only to say where to go."""

    def _check(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
        if value == "model-serving":
            raise click.UsageError(_MODEL_SERVING_REMOVED.format(command=command))
        return value

    return _check
```

Add `callback=_reject_model_serving("<command>")` to each of the eight `--target` options, using the command string from the test table. Check that `Callable` is already imported in `cli.py`; add `from collections.abc import Callable` under `TYPE_CHECKING` if needed. Click processes options in invocation order, so a `--target` given on the command line runs its callback before a later "Missing option" check. That is Review Focus 1, and the test pins it.

- [ ] **Step 4: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_model_serving_removed.py -q -p no:cacheprovider`
Expected: 8 passed.

- [ ] **Step 5: Commit** (its own command): `feat: --target model-serving names Databricks Apps (Model Serving removed)`

### Task 2: `hot-swap` and canary default to Apps; their serving-only options go

**Files:** `python/src/apx_agent/cli.py` (`agents hot-swap` ~13545; the five canary commands ~16152–16831); `python/tests/test_model_serving_removed.py`.

- [ ] **Step 1: Find the Apps implementations** that each command dispatches to when `--target apps`:

```bash
cd python && grep -n "_hot_swap_apps_cli\|from ._canary_apps import\|_canary_apps\." src/apx_agent/cli.py | head -20
```

Note the function each command calls on its Apps branch (for `hot-swap`, `_hot_swap_apps_cli`).

- [ ] **Step 2: Write the failing test** (append). Use the function names found in Step 1.

```python
def test_hot_swap_defaults_to_apps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(cli, "_hot_swap_apps_cli", lambda **kw: calls.append(kw))
    result = CliRunner().invoke(cli.main, ["agents", "hot-swap", "--llm-endpoint", "databricks-claude-sonnet-4-6"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1


@pytest.mark.parametrize("sub", ["status", "deploy", "promote", "rollback", "analyze"])
def test_canary_has_no_model_serving_only_options(sub: str) -> None:
    command = cli.canary.commands[sub]
    names = {opt for p in command.params for opt in getattr(p, "opts", [])}
    assert not names & {"--endpoint", "--workload-size", "--no-scale-to-zero"}, names
    target = next(p for p in command.params if p.name == "deploy_target")
    assert target.default == "apps"
```

If `_hot_swap_apps_cli` takes positional arguments, match its real signature in the lambda (check with `grep -n "def _hot_swap_apps_cli" -A6 src/apx_agent/cli.py`). If a canary subcommand's serving-only option set differs, take it from the inventory in the spec's PR A table: `--endpoint`, `--version` (only where the Apps branch doesn't use it), `--workload-size`, `--no-scale-to-zero`.

- [ ] **Step 3: Run to verify they fail.** Expected: the defaults are `model-serving`, and the serving options exist.

- [ ] **Step 4: Implement.**
  - Set `default="apps"` on the six `--target` options (`hot-swap` and the five canary commands).
  - Remove each serving-only option and its parameter, but only after `grep`ping the function body to confirm the Apps branch never reads it. If one is read on the Apps path, keep it and ledger a ruling.
  - Leave the serving branches' bodies in place; PR B deletes them. Where a removed parameter was used only inside the serving branch, that branch is now unreachable (`--target model-serving` errors in Task 1). Replace its body with `raise AssertionError("unreachable: Model Serving removed")` so pyright stays clean, and ledger it for PR B.
  - Update the `help=` text to name only Apps.

- [ ] **Step 5: Run.** `uv run pytest tests/test_model_serving_removed.py tests/test_canary_apps.py tests/test_hot_swap_apps.py -q -p no:cacheprovider`. Expected: PASS.
- [ ] **Step 6: Commit** (its own command): `feat: hot-swap and canary default to Apps`

### Task 3: `deploy` and `scaffold` lose their serving surface

**Files:** `python/src/apx_agent/cli.py`:
- the `deploy` options ~6949–7211;
- `_MODEL_SERVING_ONLY_DEPLOY_FLAGS` ~6923;
- the `_detect_target` model-serving rule ~516–533, and `_RUN_MODULE_BY_TARGET` ~492;
- the `scaffold` interactive prompt ~3866 and validation ~4744.

Tests: `python/tests/test_model_serving_removed.py`.

- [ ] **Step 1: Write the failing tests** (append)

```python
_REMOVED_DEPLOY_FLAGS = ["--no-deploy", "--no-publish-tools", "--no-set-uc-tags", "--agent-name", "--capture-env-vars",
                         "--allow-env-var", "--scale-to-zero", "--workload-size", "--env-suffix", "--version", "--hot-swap"]


@pytest.mark.parametrize("flag", _REMOVED_DEPLOY_FLAGS)
def test_removed_deploy_flag_is_an_unknown_option(flag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["agents", "deploy", flag, "x"])
    assert result.exit_code == 2 and "No such option" in result.output, result.output


def test_an_old_serving_layout_detects_apps(tmp_path: Path) -> None:
    # The flat agent.py + app.py layout used to auto-detect Model Serving.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    (tmp_path / "agent.py").write_text("from apx_agent import Agent\nagent = Agent(name='p', tools=[])\n")
    (tmp_path / "app.py").write_text("from apx_agent import create_app\nfrom agent import agent\napp = create_app(agent)\n")
    assert cli._detect_target(tmp_path) == "apps"
```

Check `_detect_target`'s real signature and return values first (`sed -n 500,540p src/apx_agent/cli.py`), and adjust the call and the expected value to the Apps return it uses.

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
  - **Per flag:** run `grep -n "<param_name>" src/apx_agent/cli.py` inside `deploy`'s body. If it's read only in the serving branch, remove the `@click.option` and the parameter. Do the same check for `--name`, `--model` and `--experiment`; they stay if the Apps branch reads them. Ledger each decision.
  - **`_MODEL_SERVING_ONLY_DEPLOY_FLAGS`:** delete it, along with the Apps-branch warning that iterated over it.
  - **`_detect_target`:** delete the model-serving rule, so it returns Apps for every project. Drop the `model-serving` key from `_RUN_MODULE_BY_TARGET`.
  - **`scaffold`:** the interactive target prompt offers only `apps` (or is skipped, if it was only ever a two-way choice). Validation that branched on `model-serving` now gets it rejected by Task 1's callback first.
  - **The `deploy` serving branch body:** same as Task 2. It's unreachable, so replace it with the `AssertionError` placeholder and ledger it for PR B.
- [ ] **Step 4: Run.** `uv run pytest tests/test_model_serving_removed.py tests/test_deploy_apps.py tests/test_cli.py -q -p no:cacheprovider`. Expected: the new tests PASS. Failures in serving tests are expected; Task 5 handles them, so note them.
- [ ] **Step 5: Commit** (its own command): `feat: deploy and scaffold drop their Model Serving options`

### Task 4: Remove `agents cost`, `logs --endpoint`, `delete --endpoint` and `endpoints`

**Files:** `python/src/apx_agent/cli.py`:
- `agents cost` ~15185–15282;
- `agents logs` `--endpoint/--served-model/--build` ~12550;
- `agents delete --endpoint` ~14562 and the teardown ~14880;
- the `endpoints` group ~18404–18428;
- the doctor (`_doctor.py` ~911, ~1117) and fleet (~15880, ~15929) hint text.

Tests: `python/tests/test_model_serving_removed.py`.

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_serving_only_commands_are_gone() -> None:
    assert "cost" not in cli.agents.commands
    assert "endpoints" not in cli.main.commands


@pytest.mark.parametrize(("sub", "gone"), [("logs", {"--endpoint", "--served-model", "--build"}), ("delete", {"--endpoint"})])
def test_serving_only_options_are_gone(sub: str, gone: set[str]) -> None:
    names = {opt for p in cli.agents.commands[sub].params for opt in getattr(p, "opts", [])}
    assert not names & gone, names
    assert "--app" in names


def test_advertise_still_registers_a_serving_endpoint_peer() -> None:
    # Kept by the spec: advertising an existing serving endpoint is not hosting one.
    opt = next(p for p in cli.agents.commands["advertise"].params if p.name == "endpoint_type")
    assert "model-serving" in opt.type.choices
```

Check the real attribute names (`cli.agents`, `cli.main`, `advertise`'s parameter name) with `grep -n '^def agents\b\|command("advertise")' -A3 src/apx_agent/cli.py` before running.

- [ ] **Step 2: Run to verify they fail.** The last test passes already; it guards against over-deletion.
- [ ] **Step 3: Implement.**
  - Delete the `agents cost` command, the `endpoints` group and its two commands, and the three `logs` options and one `delete` option, together with the branches that only they reached.
  - `_resolve_served_model_name` and the endpoint teardown stay as dead code for PR B, if pyright allows. If not, delete them now and ledger it.
  - Rewrite the doctor and fleet hints to name Apps.
- [ ] **Step 4: Run.** `uv run pytest tests/test_model_serving_removed.py tests/test_doctor.py tests/test_fleet.py -q -p no:cacheprovider`. Expected: the new tests PASS.
- [ ] **Step 5: Commit** (its own command): `feat: remove agents cost, endpoints, and serving-only logs/delete options`

### Task 5: Serving tests, gate, changelog, PR A

- [ ] **Step 1: Find the work list.** Run the full suite and save the `FAILED` ids to the workspace (`failing.txt`). Each failure is a test of a removed surface, or a test that used a removed flag incidentally.
- [ ] **Step 2: Handle each failing test:**
  - **It tested a removed surface** (serving deploy, scaffold, `cost`, `endpoints`, `logs --endpoint`, `delete --endpoint`, serving hot-swap or canary): delete it and ledger it by name.
  - **It used a removed flag or the serving default incidentally** (e.g. a canary test that relied on `--target` defaulting to `model-serving`): update it to the Apps form, keeping its intent.
  - **It was a serving library test that still passes against still-present code** (e.g. `test_canary.py` calling `_canary.deploy_canary` directly): leave it for PR B.
- [ ] **Step 3: Gate** (Global Constraints, including the credential-free run). Expected: green.
- [ ] **Step 4: Changelog** under `### Removed`:

```markdown
- **Model Serving deploy target.** apx-agent deploys agents to Databricks Apps only. `--target model-serving` on `agents deploy`, `agents scaffold`, `agents hot-swap` and the `canary` commands now fails with a message naming `--target apps`; `hot-swap` and `canary` default to Apps. Removed: `agents cost`, the `endpoints` group, `agents logs --endpoint/--served-model/--build`, `agents delete --endpoint`, and the serving-only `agents deploy` flags (`--no-deploy`, `--no-publish-tools`, `--no-set-uc-tags`, `--agent-name`, `--capture-env-vars`, `--allow-env-var`, `--scale-to-zero`, `--workload-size`, `--env-suffix`, `--version`, `--hot-swap`). LLM and embedding endpoints the agent calls are unaffected.
```

- [ ] **Step 5: Commit** (its own command), then run a fresh final review of the branch, then the PR with auto-merge. Clean up the worktree after it merges.

---

## PR B — dead code

### Task 6: Delete the serving deploy path and `log_agent`

**Files:**
- `python/src/apx_agent/cli.py`:
  - the `deploy` serving branch (the `AssertionError` placeholder from Task 3, and the block it replaced, ~7361–7817);
  - `_emit_serving_deploy_plan` ~8019–8107;
  - `_serving_health_gate` ~8109–8199.
- `python/src/apx_agent/_chat_agent.py`: `log_agent` ~1212–1322.
- `python/src/apx_agent/_resources.py`: `mlflow_resources_for` ~380–427.
- `python/src/apx_agent/__init__.py`: the `log_agent` export.
- Test: `python/tests/test_model_serving_removed.py`.

- [ ] **Step 1: Write the failing guard** (append)

```python
def test_no_public_model_serving_hosting_api() -> None:
    import apx_agent

    for name in ("log_agent", "hot_swap_model", "HotSwapResult", "get_active_override",
                 "deploy_canary", "cost_for_endpoint", "cost_for_agent"):
        assert not hasattr(apx_agent, name), name
    src = (Path(cli.__file__)).read_text()
    for symbol in ("_emit_serving_deploy_plan", "_serving_health_gate", "_scaffold_model_serving",
                   "_hot_swap_model_serving", "_resolve_served_model_name", "mlflow_resources_for"):
        assert symbol not in src, symbol
```

Before running, check which serving canary names `__init__.py` actually exports (`grep -n "canary\|hot_swap\|cost_for\|log_agent" src/apx_agent/__init__.py`), and set the first list to exactly those names.

- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Delete** the code listed under Files, plus whatever tests become unimportable (`test_log_agent_experiment.py`, and the `log_agent` cases in `test_chat_agent.py` ~344, ~390). Ledger them. Then run `uv run pyright src/apx_agent/` to catch dangling imports.
- [ ] **Step 4: Run** the guard, plus `uv run pytest tests/test_chat_agent.py tests/test_resources.py tests/test_deploy_apps.py -q -p no:cacheprovider`. The guard stays red on the names Task 7 removes; check the Task 6 names pass. Then **commit** (its own command): `refactor: delete the Model Serving deploy path and log_agent`.

### Task 7: Delete the serving scaffold, canary, hot-swap and cost code

**Files:**
- `cli.py`: `_scaffold_model_serving` ~4015 and templates ~1389–1460; `_hot_swap_model_serving` ~13635; `_resolve_served_model_name`; the canary serving branches; the endpoint teardown.
- `_canary.py`: delete it. First move `_percentile` into `_eval_sweep.py` and import it from there in `_dev.py`.
- `_hot_swap.py`: delete `hot_swap_model`, `HotSwapResult` and `get_active_override`. If that empties the module, delete it.
- `_cost.py`: delete `cost_for_endpoint` and `cost_for_agent`; keep `usd_for_token_usage`.
- `__init__.py`: remove those exports.

Tests: `python/tests/test_model_serving_removed.py`.

- [ ] **Step 1: Write the failing test** for Review Focus 4 (append)

```python
def test_the_runtime_model_override_still_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    # Apps hot-swap sets this env var; the responses runtime must keep honoring it.
    from apx_agent._responses_agent import _resolve_model

    monkeypatch.setenv("APX_AGENT_MODEL_OVERRIDE", "databricks-claude-opus-4-7")
    assert _resolve_model("databricks-claude-sonnet-4-6") == "databricks-claude-opus-4-7"
```

Check `_resolve_model`'s real signature (`grep -n "def _resolve_model" -A10 src/apx_agent/_responses_agent.py`) and the env var's exact name. Adjust the call; the point is that the override wins.

- [ ] **Step 2: Run.** Expected: this test PASSES already. It's a regression pin that must stay green through the deletions. The guard from Task 6 FAILS on the names this task removes.
- [ ] **Step 3: Delete** the code listed under Files. Delete the tests that only cover it, and ledger them:
  - `test_canary.py`;
  - the serving tests in `test_hot_swap.py`, keeping its `APX_AGENT_MODEL_OVERRIDE` runtime tests (~25–54);
  - `test_cost.py`'s endpoint tests;
  - the serving case in `test_scaffold_templates.py` (~27).

  Run `uv run pyright src/apx_agent/` after each module.
- [ ] **Step 4: Run.** `uv run pytest tests/test_model_serving_removed.py tests/test_hot_swap.py tests/test_hot_swap_apps.py tests/test_canary_apps.py tests/test_cost.py tests/test_budget*.py tests/test_eval_sweep*.py -q -p no:cacheprovider`. Expected: PASS, guard included. **Commit** (its own command): `refactor: delete Model Serving scaffold, canary, hot-swap and cost code`.

### Task 8: `AgentConfig.host`, stale docstrings, gate, PR B

**Files:**
- `python/src/apx_agent/_models.py`: `AgentConfig.host` Literal ~701; the `RuntimeTarget` docstring ~132.
- `python/src/apx_agent/_project_gen.py`: ~138.
- Docstrings in `_responses_agent.py` (~1–6, ~1033), `_apps_registry.py` (~9, ~20), `_readyz.py` and `_obo.py`.

- [ ] **Step 1: Write the failing test** (append)

```python
def test_host_model_serving_is_a_clear_validation_error() -> None:
    from pydantic import ValidationError

    from apx_agent._models import AgentConfig

    with pytest.raises(ValidationError, match="apps"):
        AgentConfig.model_validate({"name": "p", "host": "model_serving"})
```

- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement.**
  - Narrow `host` to `Literal["apps"]` and keep its default.
  - Check `_project_gen.py` ~138 writes only `apps`.
  - Rewrite the docstrings: `responses_agent` is "the MLflow ResponsesAgent runtime, hosted on Databricks Apps (the `samples-*` apps)", not the legacy model-serving contract. Drop "Model Serving" from the `_apps_registry`, `_readyz` and `_obo` docstrings where it describes hosting.
- [ ] **Step 4: Gate** (Global Constraints, including the credential-free run). `test_in_repo_projects.py` must pass unchanged.
- [ ] **Step 5: Changelog** `### Removed` entry: "The Model Serving hosting API (`log_agent`, `hot_swap_model`, `HotSwapResult`, `get_active_override`, `deploy_canary` …, `cost_for_endpoint`, `cost_for_agent`) is removed from the package; `AgentConfig.host` accepts only `"apps"`." List the exact names removed in Tasks 6–7. Then **commit** (its own command), run a fresh final review, and open the PR with auto-merge. Clean up after it merges.

---

## PR C — docs and examples

### Task 9: User docs

**Files:** user docs under `docs/` and `README.md` / `python/README.md`. Exclude `docs/design/`, `docs/superpowers/`, `docs/engine-scope/` and `docs/specs/`.

- [ ] **Step 1: Write the failing guard** (append to `python/tests/test_in_repo_projects.py`)

```python
def test_user_docs_do_not_offer_model_serving_hosting() -> None:
    history = ("docs/design/", "docs/superpowers/", "docs/engine-scope/", "docs/specs/")
    docs = [p for p in [*(_REPO / "docs").rglob("*.md"), _REPO / "README.md", _REPO / "python" / "README.md"]
            if not str(p.relative_to(_REPO)).startswith(history)]
    hosting = re.compile(r"--target model-serving|deploy (?:it |the agent |agents )?to (?:Mosaic AI )?Model Serving"
                         r"|apps-vs-model-serving|agents cost\b|apx-agent endpoints\b", re.I)
    hits = sorted(f"{p.relative_to(_REPO)}" for p in docs
                  if hosting.search(p.read_text()) and "Model Serving was removed" not in p.read_text())
    assert hits == [], hits
```

- [ ] **Step 1b: Write a second guard (added after PR B's review).** User docs and example READMEs must not show code that imports the removed API:

```python
_REMOVED_API = ("log_agent", "mlflow_resources_for", "hot_swap_model", "HotSwapResult", "get_active_override",
                "APX_MODEL_OVERRIDE_ENV", "cost_for_endpoint", "cost_for_agent", "CostBreakdown", "CanaryConfig",
                "CanaryReport", "VersionMetrics", "analyze_canary", "deploy_canary", "get_canary_config",
                "promote_canary", "rollback_canary")


def test_user_docs_do_not_use_the_removed_api() -> None:
    history = ("docs/design/", "docs/superpowers/", "docs/engine-scope/", "docs/specs/")
    docs = [p for p in [*(_REPO / "docs").rglob("*.md"), _REPO / "README.md", _REPO / "python" / "README.md",
                        *(_REPO / "python" / "examples").glob("*/README.md")]
            if not str(p.relative_to(_REPO)).startswith(history)]
    hits = sorted(f"{p.relative_to(_REPO)}:{n}" for p in docs
                  for name in _REMOVED_API if re.search(rf"\b{name}\b", p.read_text()) for n in [name])
    assert hits == [], hits
```

Known hits at PR B: `docs/tools/overview.md` and `docs/safety/compliance.md` (`log_agent` imports); `docs/reference/cost-tracking.md` (`cost_for_agent`; delete the page or reduce it to `usd_for_token_usage`); `docs/evaluate/advanced.md`, `docs/tools/custom-tools.md`, `docs/running/tracing.md`, `docs/evaluate/model-sweeps.md`, `python/examples/slack-uc-mcp/README.md`. Also fix `docs/reference/pyproject-toml.md`'s `# apps | model_serving` host comment.

- [ ] **Step 2: Run to verify it fails.** Expected: about 25 docs.
- [ ] **Step 3: Rewrite.**
  - **`docs/deploy/apps-vs-model-serving.md`:** delete it, and the links to it in `README.md`, `docs/README.md` and `docs/deploy/overview.md`.
  - **Deploy guides** (`docs/deploy/overview.md`, `troubleshooting.md`, `agentbricks-platform.md`): drop the serving sections.
  - **`README.md`, `python/README.md`, `docs/positioning.md`:** Apps only.
  - **`docs/get-started/{cli,quickstart,dev-ui}.md`:** drop the removed commands and flags.
  - **`docs/get-started/migration.md`:** add a short "Model Serving was removed" section naming `--target apps`.
  - **`docs/running/runtime-targets.md` and others:** where they tie `responses_agent` to Model Serving, describe it as an Apps runtime.
  - **Keep** every mention of LLM or embedding endpoints the agent calls.
  - **Check examples:** every deploy example must use flags that exist. Run `uv run apx-agent agents deploy --help` and confirm each flag shown.
- [ ] **Step 4: Run.** Run the guard, `tests/test_eval_run_customer_path.py` (it executes `docs/evaluate/overview.md`'s blocks) and `make check`. Expected: PASS. **Commit** (its own command): `docs: Apps is the one deploy target`.

### Task 10: Examples, samples, gate, PR C

**Files:**
- `python/examples/{shortage-intelligence-agent,customer_triage,data-triage-agent,memory_demo}/README.md`;
- `python/examples/memory_demo/databricks.yml` (comment);
- `python/samples-*/agent.py` module docstrings.

Not the vendored `python/examples/databricks-skills/`, `databricks-mcp-server` or `databricks-tools-core`.

- [ ] **Step 1: Extend the guard** (in `test_user_docs_do_not_offer_model_serving_hosting`): add `*(_REPO / "python" / "examples").glob("*/README.md")` and `*(_REPO / "python").glob("samples-*/agent.py")` to the files checked, excluding the vendored directories. Run it; expected: FAIL listing those files.
- [ ] **Step 2: Rewrite.**
  - Remove the "Option: Model Serving" sections and the `agents deploy --name … --experiment …` serving walkthroughs.
  - Each example's deploy step uses `--target apps`.
  - Each sample's docstring drops its `--target model-serving` mention.
- [ ] **Step 3: Gate** (Global Constraints, including the credential-free run). `test_in_repo_projects.py` must pass unchanged.
- [ ] **Step 4: Changelog** `### Docs` entry: "Docs and examples describe Databricks Apps as the one deploy target; `docs/deploy/apps-vs-model-serving.md` is removed." Then **commit** (its own command), run a fresh final review, and open the PR with auto-merge. Clean up after it merges, and update memory.

---

## Out of this plan

- Renaming `endpoint_predict_fn` / `--endpoint-url`.
- An Apps equivalent of `agents cost`.
- Removing `model-serving` as an accepted `--target` value. That comes in a later release.
- `advertise` / `publish` `--endpoint-type model-serving`, which registers an existing serving endpoint as a peer.
