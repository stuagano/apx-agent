# Sub-project 3d: `[tool.apx.agent]` is an error — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `[tool.apx.agent]` table in `pyproject.toml` or `.apx.local` fails loudly with a message naming `apx-agent agents migrate-config`, the way #944 rejected `[[tool.apx.tools]]`. `template =` and `module =` stop being runtime config. Every setting keeps working through its home: kwargs on the agent in `agent.py`, `[tool.apx.deploy]`, `[tool.apx]`, or a YAML spec.

**Architecture:** Three PRs, in order. Each merges before the next is cut from `main`; never stack them (memory: repo auto-merge collapses stacked PRs).

| PR | Contents | Why this order |
|---|---|---|
| **A: `finalize_agent` resolves the agent's settings** (Task 1) | A live bug on `main`, see below | Independent of the rest, and must land before the table can be removed |
| **B: messages and docs point at the new homes** (Tasks 2–3) | User-facing text only; the table still works | Once C ships, every error and help string already tells users the right thing |
| **C: reject the table; remove `template =` and `module =`** (Tasks 4–8) | The error, the `migrate-config` bypass, test fixture conversion, and deletion of the code paths that only served the table | Last, because it's the breaking change |

**The live bug (found while surveying, reproduced).** The Apps `responses_agent` launcher that `scaffold` writes (`cli.py`, the start_server template at ~1665) calls `finalize_agent(agent, _load_agent_config())`. The hello-world, payroll, hubspot and `samples-*` copies do the same. `_load_agent_config` returns only TOML/static fields, and `finalize_agent` never calls `resolve_settings`. So settings declared on the agent are never applied on that path: `guardrails`, `service_policies`, `bindings` and `vector_search_index`.

Reproduced on e77a23f8: an `Agent(name="p", tools=[], guardrails={"injection_detection": True})` in a project with no table gets **0** input guards from `finalize_agent(agent, _load_agent_config(...))`, and **1** when the config is resolved first.

Since #997, scaffolds put settings in `agent.py`, so a user following the docs silently loses their guardrails. 3d would make `agent.py` the only place to declare them. `setup_agent` (`_wiring.py:783`) and the generated-project launcher (`_project_gen.py` ~171) already resolve; `finalize_agent` is the one seam that doesn't.

**Tech Stack:** Python ≥3.10, pytest, ctk.

**Spec:** `docs/design/agent-toml-source-of-truth.md`, item 3d ("`[tool.apx.agent]` is an error naming `migrate-config`, as #944 did for `[[tool.apx.tools]]`. `template =` and `module =` are removed."), and the notes "Left for 3d" (3b-2) and the `.apx.local` precedence note (3b-1).

## Global Constraints

- **Each PR has its own Orca worktree, branch and gate.**
  - PR A: this worktree (`apx-reject-agent-table`), on branch `stuartg-dev/apx-reject-agent-table`.
  - PRs B and C: new worktrees cut from the then-current `origin/main` once the previous PR has merged.
- **Running tests and the gate:**
  - Run tests from `python/`: `cd python && uv run pytest … -q -p no:cacheprovider`.
  - Gate:
    - `make check`;
    - `pre-commit run --files <changed files>`;
    - `uv run pyright src/apx_agent/`;
    - the PR CI subset under xdist, twice. In zsh: ``paths=$(BASE=origin/main HEAD=HEAD EVENT_NAME=pull_request PR_PYTEST_CHANGED_FILES="$(git diff --name-only origin/main...HEAD; git diff --name-only)" bash ../scripts/pr-pytest-paths.sh); uv run pytest ${=paths} -n auto --timeout=90 --timeout-method=thread``.
- **Lint bans:**
  - `.get(k, "")` and `x or ""`;
  - `getattr(x, "a", "<str literal>")`;
  - `object` annotations;
  - `str = ""` defaults;
  - invented env defaults;
  - `tuple[...]` returns;
  - unconditional `@pytest.mark.skip`.
- **Commits:**
  - end with `Co-authored-by: Isaac <no-reply@databricks.com>`;
  - run `git commit --quiet --message …` alone in its own command;
  - never `git stash`;
  - before committing, check that any `uv.lock` has 0 `pypi-proxy` lines.
- **Public repo:** no roadmap in commits or PRs. Design history in `docs/design/` and `docs/superpowers/` is **not** rewritten; it records what was true then.
- **Homes for every setting.** Checked on e77a23f8: every `AgentConfig` field has one.

| Setting | Home |
|---|---|
| `model`, `identity`, `target`, `recovery`, `gateway`, `service_policies`, `guardrails`, `knowledge`, `vector_search_index`, `bindings`, `url`, `registry`, `api_prefix`, `examples`, `workflows`, `data`, `executor`, `example`, `description` | keyword argument on the root agent in `agent.py` (or `agent.configure(...)`) |
| `instructions`, `temperature`, `max_tokens`, `max_iterations`, `sub_agents`, `memory`, `session` | agent constructor argument |
| `name`, `host`, deploy fields, `registered_model`, `catalog`, `schema`, `supervisor_id`, `registry_table`, `tools_table` | `[tool.apx.deploy]` |
| `profile`, `experiment` | `[tool.apx]` (`profile` in `.apx.local`) |
| `agents`, `root`, `template`, `tools`, `skills`, `data`, `executor`, `workflows`, `example` | YAML spec (input to `generate_project`) |

## Review Focus

1. **A deployed app still carrying `[tool.apx.agent]`.** Expected: it fails at boot with the migrate-config message, not a traceback from deep inside the loader, and not a silent default config. Pinned in Task 4.
2. **`migrate-config` on a legacy project after 3d.** Expected: it still reads the table, migrates it, and verifies the result. The bypass is used by migrate-config alone. Pinned in Task 4.
3. **`.apx.local` with only a legacy `[tool.apx.agent] profile`.** Expected: the same error, naming migrate-config, which already moves it (`migrate_local`). Pinned in Task 4.
4. **A YAML spec with `template:`** (the coworker gallery). Expected: still generates a project. Only the pyproject/runtime `template =` path goes away. Pinned in Task 7.
5. **Settings declared on the agent and in the config.** For example, when `setup_agent` calls `finalize_agent` after resolving. Expected: `resolve_settings` is idempotent on an already-resolved config, with no false `SettingsConflict`. Pinned in Task 1.

---

## PR A — `finalize_agent` resolves the agent's settings

### Task 1: Settings declared on the agent apply on every launcher

**Files:**
- Modify: `python/src/apx_agent/_wiring.py`, `finalize_agent` (~550)
- Test: `python/tests/test_wiring.py`

**Interfaces:**
- Produces: `finalize_agent(agent, config=None, pyproject_path=None, ws=None)` applies `resolve_settings(agent, config)` before every config→instance step. Callers no longer need to resolve first.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_wiring.py`)

```python
def test_finalize_applies_guardrails_declared_on_the_agent(tmp_path: Path) -> None:
    # The Apps responses launcher calls finalize_agent(agent, _load_agent_config()): TOML only.
    from apx_agent._inspection import _load_agent_config
    from apx_agent._wiring import finalize_agent

    pp = tmp_path / "pyproject.toml"
    pp.write_text('[project]\nname = "p"\nversion = "0"\n\n[tool.apx.deploy]\nname = "p"\n')
    agent = Agent(name="p", tools=[], guardrails={"injection_detection": True})
    finalize_agent(agent, _load_agent_config(pyproject_path=pp), pyproject_path=str(pp))
    assert len(agent._input_guardrails) == 1


def test_finalize_on_an_already_resolved_config_is_not_a_conflict(tmp_path: Path) -> None:
    from apx_agent._inspection import _load_agent_config
    from apx_agent._settings import resolve_settings
    from apx_agent._wiring import finalize_agent

    pp = tmp_path / "pyproject.toml"
    pp.write_text('[project]\nname = "p"\nversion = "0"\n\n[tool.apx.deploy]\nname = "p"\n')
    agent = Agent(name="p", tools=[], model="m", guardrails={"injection_detection": True})
    config = resolve_settings(agent, _load_agent_config(pyproject_path=pp))
    finalize_agent(agent, config, pyproject_path=str(pp))
    assert len(agent._input_guardrails) == 1
```

Before running, check how `test_wiring.py` imports `Agent` and `Path`, and whether `_input_guardrails` is the attribute the guards land on: `grep -n "_input_guardrails" src/apx_agent/_wiring.py | head`.

- [ ] **Step 2: Run to verify**

Run: `cd python && uv run pytest tests/test_wiring.py -q -p no:cacheprovider -k "declared_on_the_agent or already_resolved"`
Expected:
- the first test FAILS (`0 == 1`): this is the bug;
- the second PASSES already; it guards the fix against double-resolution.

- [ ] **Step 3: Implement.** In `finalize_agent`, after the `if config is None:` load:

```python
    if config is not None:
        from ._settings import resolve_settings  # noqa: PLC0415

        config = resolve_settings(agent, config)  # settings declared on the agent apply on every launcher
```

Leave the per-step `if config is not None:` guards as they are. `_apply_leaf_identities` already resolves internally; it stays correct on a resolved config.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_wiring.py tests/test_agent_settings.py tests/test_in_repo_projects.py tests/test_scaffold_templates.py tests/test_memory_wiring.py -q -p no:cacheprovider`
Expected: PASS. If a test asserted the old behavior (a Python-declared setting ignored by `finalize_agent`), it was pinning the bug. Update it, and list it in the ledger.

- [ ] **Step 5: Commit** (its own command), then gate, changelog `### Fixed` entry, PR A:

```markdown
- **Settings declared on the agent apply on the Apps `responses_agent` launcher.** `finalize_agent` now merges the agent's own settings, so `guardrails=`, `service_policies=`, `bindings=` and `vector_search_index=` on the agent in `agent.py` take effect in scaffolded `agent_server/start_server.py` apps; before, only the TOML's applied there.
```

```bash
git commit --quiet --message "fix: finalize_agent applies settings declared on the agent

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

## PR B — messages and docs point at the new homes

### Task 2: User-facing strings in the package

**Files:** `python/src/apx_agent/*.py`. Messages, `help=` text, the generated start_server template's comments (`cli.py` ~1660–1680), and the in-repo `agent_server/start_server.py` copies, which carry the same comments.

- [ ] **Step 1: Write the failing guard** (append to `tests/test_in_repo_projects.py`)

```python
def test_no_user_facing_text_tells_users_to_write_the_agent_table() -> None:
    # migrate-config's own messages and the legacy-compat code may name the table; nothing else.
    allowed = {"_migrate_config.py", "_tool_config.py"}
    src = _REPO / "python" / "src" / "apx_agent"
    hits = sorted(f"{p.name}:{i}" for p in src.glob("*.py") if p.name not in allowed
                  for i, line in enumerate(p.read_text().splitlines(), 1)
                  if "tool.apx.agent" in line and not line.lstrip().startswith("#"))
    assert hits == [], hits
```

- [ ] **Step 2: Run to verify it fails.** Run: `uv run pytest tests/test_in_repo_projects.py -q -p no:cacheprovider -k user_facing_text`. Expected: FAIL, listing roughly 150 hits.

- [ ] **Step 3: Rewrite each hit** using the homes table in Global Constraints:
  - **A message about a runtime setting:** name the agent keyword argument. Example: `"set url in [tool.apx.agent]"` becomes `"set url= on the agent in agent.py"`.
  - **A message about a deploy or registration key:** name `[tool.apx.deploy]`.
  - **A message about `profile` or `experiment`:** name `[tool.apx]`.
  - **Help text that describes reading "the [tool.apx.agent] block":** say "the project's settings".
  - **Docstrings and comments:** update only where they would mislead a reader. The guard ignores `#` comments; docstrings count, so it forces those.
  - **The `migrate-config` command's own help and messages** (`cli.py` ~5015–5141) keep naming the table they migrate. Add `cli.py` lines inside `migrate_config` / `migrate_agent_toml` to the guard's allow-list by function: compute line ranges from the `def` lines, not hard-coded numbers.
  - **The `_read_apx_agent_config` legacy-merge text and the `_fold_deploy_table` conflict text** stay until PR C deletes them. Allow them the same way, with a `# ponytail: until 3d PR C` comment.

  Rule on each borderline case in the ledger.

- [ ] **Step 4: Run to verify.** Run `make check`. Expected: PASS. Tests that assert an exact message string move to the new text with the same intent; list them in the ledger.

- [ ] **Step 5: Commit** (its own command): `docs: messages and help name the new homes for agent settings`.

### Task 3: User docs

**Files:** the user-facing docs, listed by `grep -rlc "tool\.apx\.agent" docs README.md | grep -v "^docs/design/\|^docs/superpowers/"`. That's about 31 files: `docs/reference/pyproject-toml.md` (24 hits), `docs/reference/configuration.md` (19), `service-policies.md` (8), `running/sessions-and-memory.md` (6), `README.md` (5), `running/runtime-targets.md` (5), and the rest.

- [ ] **Step 1: Write the failing guard** (append to `tests/test_in_repo_projects.py`)

```python
def test_user_docs_show_the_new_homes() -> None:
    history = ("docs/design/", "docs/superpowers/")
    docs = [p for p in [*(_REPO / "docs").rglob("*.md"), _REPO / "README.md"]
            if not str(p.relative_to(_REPO)).startswith(history)]
    hits = sorted(str(p.relative_to(_REPO)) for p in docs
                  if "tool.apx.agent" in p.read_text() and "migrate-config" not in p.read_text())
    assert hits == [], hits
```

A doc may still mention the old table, but only alongside the migration path (for example a "Migrating from `[tool.apx.agent]`" note).

- [ ] **Step 2: Run to verify it fails.** Expected: about 31 files.
- [ ] **Step 3: Rewrite each page's examples** to the new homes:
  - TOML settings blocks become `agent.py` kwargs plus a `[tool.apx.deploy]` table;
  - graph examples become YAML specs, as #999 did;
  - `module = "agent:agent"` lines are deleted.

  `docs/reference/pyproject-toml.md` gets a short "Removed: `[tool.apx.agent]`" section that names `apx-agent agents migrate-config` and links the homes table. `docs/get-started/migration.md` gets the same note. Don't invent behavior: for each example, check that the kwarg exists (`CompositeSettings` / the constructor signatures).
- [ ] **Step 4: Run to verify.** The guard passes, and `make check` passes; `docs` tests include the CHANGELOG/doc-drift checks, if any.
- [ ] **Step 5: Commit** (its own command): `docs: user docs show settings on the agent and in [tool.apx.deploy]`. Then gate, changelog `### Docs` entry, PR B.

---

## PR C — reject the table; remove `template =` and `module =`

### Task 4: The error, and migrate-config's bypass

**Files:**
- Modify:
  - `python/src/apx_agent/_tool_config.py` (`reject_legacy_tool_config`, ~216);
  - `_inspection.py` (`_load_agent_config`, `_agent_config_fields`);
  - `cli.py` (`_read_apx_agent_config` ~214, `migrate_config` ~5041, `migrate_agent_toml` ~5015).
- Test: `python/tests/test_agent_table_removed.py` (create)

**Interfaces:**
- Produces:
  - `reject_legacy_tool_config(pyproject_path=None, *, allow_agent_table=False)`, which raises `ToolConfigError` (a `ValueError`) on `[tool.apx.agent]`;
  - `_load_agent_config(..., allow_agent_table: bool = False)`;
  - `_read_apx_agent_config(pyproject_path=None, *, allow_agent_table: bool = False)`.

  Only `migrate_config` passes `True`.

- [ ] **Step 1: Write the failing tests** (`tests/test_agent_table_removed.py`)

```python
"""[tool.apx.agent] is an error naming migrate-config (sub-project 3d)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from apx_agent import cli
from apx_agent._inspection import _load_agent_config
from apx_agent._tool_config import ToolConfigError

_LEGACY = '[project]\nname = "p"\nversion = "0"\n\n[tool.apx.agent]\nname = "p"\nmodel = "m"\n'


def test_the_table_is_an_error_naming_migrate_config(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY)
    with pytest.raises(ToolConfigError, match=r"\[tool\.apx\.agent\] is no longer supported.*apx-agent agents migrate-config"):
        _load_agent_config(pyproject_path=tmp_path / "pyproject.toml")


def test_a_legacy_apx_local_table_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    (tmp_path / ".apx.local").write_text('[tool.apx.agent]\nprofile = "dev"\n')
    with pytest.raises(click.ClickException, match="migrate-config"):
        cli._read_apx_agent_config(tmp_path / "pyproject.toml")


def test_finalize_rejects_the_table_at_boot(tmp_path: Path) -> None:
    from apx_agent import Agent
    from apx_agent._wiring import finalize_agent

    (tmp_path / "pyproject.toml").write_text(_LEGACY)
    with pytest.raises(ToolConfigError, match="migrate-config"):
        finalize_agent(Agent(name="p", tools=[]), pyproject_path=str(tmp_path / "pyproject.toml"))


def test_migrate_config_still_migrates_a_legacy_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY)
    (tmp_path / "agent.py").write_text('from apx_agent import Agent\nagent = Agent(name="p", tools=[], model="m")\n')
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", "--no-verify"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.main, ["agents", "migrate-config"])
    assert result.exit_code == 0, result.output
    assert "removed [tool.apx.agent]" in result.output
    assert _load_agent_config(pyproject_path=tmp_path / "pyproject.toml").name == "p"
```

Add `import click`. The `--no-verify` here is on a throwaway test repo's commit, not this repo's. If a hook still objects, set `core.hooksPath=/dev/null` with `-c` instead. Check how `test_migrate_config.py` already sets up git (`test_two_runs_migrate_a_real_shaped_project`) and copy that.

- [ ] **Step 2: Run to verify.** Run: `cd python && uv run pytest tests/test_agent_table_removed.py -q -p no:cacheprovider`. Expected: the three rejection tests FAIL (no error yet); the migrate test PASSES.

- [ ] **Step 3: Implement.**
  - **`reject_legacy_tool_config`:** after the `tools` check, add:

```python
    if "agent" in apx and not allow_agent_table:
        raise ToolConfigError(
            f"{path}: [tool.apx.agent] is no longer supported. Run `apx-agent agents migrate-config` "
            f"in {path.parent} to move its settings onto the agent in agent.py and into "
            "[tool.apx.deploy] / [tool.apx], then commit the result."
        )
```

  - **`_load_agent_config(..., allow_agent_table=False)`:** pass it to `reject_legacy_tool_config`.
  - **`_read_apx_agent_config(..., allow_agent_table=False)`:** raise `click.ClickException` with the same guidance when the pyproject or `.apx.local` has an `agent` table and the flag is off.
  - **`migrate_config`:** pass `allow_agent_table=True` to every `_load_agent_config` / `_read_apx_agent_config` call it makes (`before`, `after`, `_meta`, `_apply_local`'s `before_flat`).
  - **`migrate_agent_toml`:** read through `_effective_config(cwd, module)` so the model and identity come from the agent. Its "No [tool.apx.agent] to migrate." becomes "No pyproject.toml here."
  - **CLI commands surface the error:** check that a `ToolConfigError` (a `ValueError`) raised in a CLI command reaches the user as a clean `Error:` line, not a traceback. Try `apx-agent agents status` in a legacy tmp project. If it doesn't, convert it at the shared seam (`_effective_config` / `_load_finalized_agent`) and say so in the ledger.

- [ ] **Step 4: Run to verify.** Run `uv run pytest tests/test_agent_table_removed.py tests/test_migrate_config.py -q -p no:cacheprovider`. Expected: PASS.

- [ ] **Step 5: Commit** (its own command): `feat: [tool.apx.agent] is an error naming migrate-config`.

### Task 5: Convert the test fixtures

**Files:** about 39 test files under `python/tests` that write `[tool.apx.agent]` (measured on e77a23f8: 43 files and 271 occurrences, minus the keep-list).

**Keep-list:** these test the migration or legacy compat, and keep the table:
- `test_migrate_config.py`;
- `test_agent_table_removed.py`;
- `test_cli_metadata.py` (legacy vs. new-home cases; with the flag, or rewritten to assert the error);
- `test_deploy_table.py` (the legacy/new-home conflict cases become "rejected");
- `test_pre349_config_compat_reality_ctk.py`.

- [ ] **Step 1: The guard (RED).** Run `cd python && uv run pytest -q -p no:cacheprovider 2>&1 | grep '^FAILED' > <workspace>/failing.txt; wc -l < <workspace>/failing.txt`. Expected: many FAILs with the migrate-config message; that file is the work list.

- [ ] **Step 2: Convert one file at a time, keeping each test's intent.** Pick the pattern that fits:
  - **(a) The test needs an `AgentConfig`** (knob application, guardrails, memory wiring). Build it directly: `AgentConfig(name="t", guardrails={...})`, or `AgentConfig.model_validate({...})` for nested tables. Don't go through a pyproject.
  - **(b) The test needs a project on disk** (CLI, doctor, deploy). Write the runtime settings as kwargs in a tmp `agent.py`, and `name`/deploy/registration keys in `[tool.apx.deploy]`, the way the #997 scaffold does. `name = "x"` becomes `[tool.apx.deploy]\nname = "x"`. Use `target='...'` as a literal kwarg so the static read sees it.
  - **(c) The test pinned `[tool.apx.agent]` precedence or merge** against Python (`test_inspection.py`, `test_effective_config.py`, `test_agent_settings.py`): delete the TOML-vs-Python cases, which can no longer occur. Keep the Python-only, deploy-table and `.apx.local [tool.apx]` cases. Ledger each deleted test by name with "pinned removed behavior". This is the one place tests get deleted, and only for behavior the spec removes.
  - **(d) `template =` fixtures** (`test_wiring.py`, `test_cli.py`, `test_project_gen.py`, `test_project_gen_reality_ctk.py`, `test_scaffold_templates.py`): see Task 7. YAML-spec `template:` cases stay.
  - **(e) `module =` fixtures** (`test_doctor_durable.py:23`, `test_apps_api_deploy.py:30`): drop the line.

  After each file, run it and confirm it's green. Then continue. Commit every few files: `test: <files> declare settings on the agent, not [tool.apx.agent]`.

- [ ] **Step 3: Verify the sweep.** Run `grep -rln "tool\.apx\.agent" python/tests | sort`. Expected: only the keep-list, plus lines in other files that assert the *error message*. Then run `make check`. Expected: PASS.

### Task 6: Delete the code that only served the table

**Files:** `cli.py`, `_inspection.py`, `_doctor.py`, `_doctor_durable.py`, `_settings.py`.

Each deletion below is dead once Task 4 rejects the table. Delete each one, then run the tests that cover it (named after each item):
- `_read_apx_agent_config`: the legacy-table merge, and its "declared in both" conflict (`cli.py` ~246–256). `migrate_config` still needs the raw legacy read; keep it behind `allow_agent_table` only. Tests: `test_cli_metadata.py`.
- `_save_to_pyproject`'s `_in_legacy` branch (~652–661): writers always target the new homes. Tests: `test_cli_metadata.py`, `test_deploy_table.py`.
- `_fold_deploy_table`'s `[tool.apx.agent]` conflict branch (`_inspection.py` ~275). Tests: `test_deploy_table.py`.
- `_doctor._declared_settings` raw-table read (~532–561): read the effective config, as the rest of doctor does (3b-2). Tests: `test_doctor.py`.
- `_doctor._is_apx_project`'s `"[tool.apx.agent]"` text sniff (~1041): an apx project is one with `agent.py`, `[tool.apx]` or `[tool.apx.deploy]`. Tests: `test_doctor.py`.
- `_doctor_durable._stage_config`'s presence check (~287–308): load via `_load_agent_config`. Tests: `test_doctor_durable.py`.
- `_agent_config_fields` (`_inspection.py`): stop reading the `agent` section except under `allow_agent_table`.

`resolve_settings`'s TOML-vs-Python conflict (`_settings.py` ~79) stays. The TOML now only carries deploy-table and static fields, but the check is cheap and still guards `name`.

- [ ] Steps: delete, run the named tests (expected PASS), then commit `refactor: remove the [tool.apx.agent] read paths`.

### Task 7: `template =` and `module =` stop being runtime config

**Files:** `_wiring.py` (`resolve_agent` template branch ~683–703, `TemplateConfigError` text), `cli.py` (`_resolve_apps_uc_name` ~10518, the `TemplateConfigError` catch ~330), `_doctor.py` (`check_uc_data_source` template fallback ~747), `_dev.py` (~2903), `_doctor_durable.py` (`AGENT_MODULE_FIX` ~33), `_project_gen.py` (docstring ~15).

**Ruling (from the spec and the survey):**
- "`template =` is removed" means the **pyproject/runtime** path: building an agent from a `template = {...}` table with no `agent.py`. A pyproject can no longer carry it (Task 4).
- The **YAML spec's `template:`** stays. It's sub-project 4's input, and `generate_project` renders it into `agent.py` (`render_agent_py`'s template branch; the six `coworker_gallery/*.yaml`; `_bake_schema_into_project` and `_emit_governance_receipt`, which read the YAML's config).
- `AgentConfig.template` therefore stays as a YAML-spec field.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_agent_table_removed.py`)

```python
def test_resolve_agent_needs_an_agent_not_a_template() -> None:
    from apx_agent._models import AgentConfig
    from apx_agent._wiring import TemplateConfigError, resolve_agent

    config = AgentConfig.model_validate({"name": "p", "template": {"name": "data", "catalog": "c", "schema": "s"}})
    with pytest.raises(TemplateConfigError, match="agent.py"):
        resolve_agent(None, config)


def test_a_gallery_yaml_with_a_template_still_generates(tmp_path: Path) -> None:
    from apx_agent._project_gen import generate_project
    from apx_agent._yaml_spec import load_spec

    spec = Path(cli.__file__).parent / "coworker_gallery" / "payroll.yaml"
    generate_project(load_spec(spec, strict=False), tmp_path)
    assert "template_registry.build(" in (tmp_path / "agent.py").read_text() or "CoworkerAgent(" in (tmp_path / "agent.py").read_text()
```

Before running, check `resolve_agent`'s real signature and what `render_agent_py` emits for a gallery template, and set the second assertion to the actual output. The point is that the YAML path still generates.

- [ ] **Step 2: Run to verify.** The first FAILS: today it builds from the template. The second PASSES; it guards the YAML path.
- [ ] **Step 3: Implement.**
  - Delete `resolve_agent`'s template branch. With no agent, `TemplateConfigError("No agent to resolve: define `agent` in agent.py (YAML specs with `template:` generate one: apx-agent agents run <spec>.yaml).")`.
  - Delete the `config.get("template")` reads in `_resolve_apps_uc_name` and in doctor's `check_uc_data_source` fallback; both now get catalog/schema from `[tool.apx.deploy]` (3b-1).
  - Delete the `_dev.py` synthesize-from-template branch, if it's only reachable from a pyproject template; check its callers first and ledger the finding.
  - `module`: fix `_project_gen.py`'s docstring (it claims `module =` is written; it isn't). Reword `AGENT_MODULE_FIX` to name `agent.py`'s `agent`, not a `module =` key.
  - `_migrate_config.py` keeps its `template` / `module` rejections. They are migration guidance for legacy projects.
- [ ] **Step 4: Run to verify.** Run `uv run pytest tests/test_agent_table_removed.py tests/test_wiring.py tests/test_project_gen.py tests/test_doctor.py tests/test_cli.py tests/test_dev*.py -q -p no:cacheprovider`. Expected: PASS.
- [ ] **Step 5: Commit** (its own command): `refactor: template = and module = are no longer runtime config`.

### Task 8: Gate, changelog, PR C

- [ ] **Step 1: In-repo read-back.** `test_in_repo_projects.py` passes unchanged. Every in-repo project loads without the table; this is the 3c guarantee, and now the loader enforces it.
- [ ] **Step 2: Gate.** Run:
  - `make check`;
  - pre-commit on the changed files;
  - `uv run pyright src/apx_agent/`;
  - the xdist subset, twice.

  Expected: green.
- [ ] **Step 3: Changelog** under `### Removed`:

```markdown
- **`[tool.apx.agent]` is an error.** A `[tool.apx.agent]` table in `pyproject.toml` or `.apx.local` now fails with a message naming `apx-agent agents migrate-config`, which still migrates it: runtime settings move onto the agent in `agent.py`, deploy and registration keys to `[tool.apx.deploy]`, `profile`/`experiment` to `[tool.apx]`. A pyproject `template = {...}` no longer builds an agent (YAML specs keep `template:`), and `module =` is gone (the agent is `agent` in `agent.py`).
```

- [ ] **Step 4: Commit** (its own command), then push and open PR C.

---

## Out of this plan

- The YAML spec path and Model Serving deploy settings (sub-project 4, #152).
- Rewriting design history under `docs/design/` and `docs/superpowers/`.
- The deferred 3c minors (characterization snapshot vs. launcher path: Task 1 closes the main gap; stale comments: Task 2 covers them).
