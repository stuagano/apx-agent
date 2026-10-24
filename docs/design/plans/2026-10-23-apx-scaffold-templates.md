# Sub-project 3b-3c: `scaffold` templates write settings in `agent.py` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The model-serving and Apps `responses_agent` scaffolds stop writing `[tool.apx.agent]`. Their runtime settings become literal keyword arguments on the root agent in `agent.py`, registration keys go to `[tool.apx.deploy]`, and every scaffolded project's effective config is unchanged.

**Architecture:** Only string templates and substitutions in `cli.py` change:
- `_SCAFFOLD_PYPROJECT` / `_SCAFFOLD_AGENT` (model-serving);
- `_SCAFFOLD_APPS_PYPROJECT` and the Apps agent templates, through a new `<SETTINGS_ARG>` placeholder;
- the session and memory blocks.

TOML `instructions` that used to overlay a template's grounding become `agent.overlay_instructions(...)` lines (#996).

**Tech Stack:** Python ≥3.10, pytest, ctk.

**Spec:** `docs/design/agent-toml-source-of-truth.md`, section "Sub-project 3b-3b", subsection "`scaffold` templates (3b-3c)".

## Global Constraints

- **Running tests and the gate:**
  - Run tests from `python/`: `cd python && uv run pytest … -q -p no:cacheprovider`.
  - Gate: `make check`; `pre-commit run --files <changed files>`; CI's `uv run pyright src/apx_agent/`; the PR CI subset under xdist. In zsh: ``paths=$(BASE=origin/main HEAD=HEAD EVENT_NAME=pull_request PR_PYTEST_CHANGED_FILES="$(git diff --name-only origin/main...HEAD; git diff --name-only)" bash ../scripts/pr-pytest-paths.sh); uv run pytest ${=paths} -n auto --timeout=90 --timeout-method=thread``.
- **Lint bans:**
  - `.get(k, "")` and `x or ""`;
  - `getattr(x, "a", "<str literal>")`;
  - `object` annotations;
  - `str = ""` defaults;
  - invented env defaults;
  - `tuple[...]` returns;
  - unconditional `@pytest.mark.skip`.
- **No effective-config change.** For each scaffold variant, the scaffolded project's effective config (`resolve_settings(agent, _load_agent_config(pyproject))`) has the same `name`, `model`, `target`, `description`, `examples`, `knowledge`, `session`, `registered_model`, `catalog` and `schema` as before this change.
- **Literal kwargs only on the `agent = …(...)` constructor**, so deploy's static pre-import read (`static_agent_setting`) sees `target`, `memory` and `session`.
- **Commits** end with `Co-authored-by: Isaac <no-reply@databricks.com>`. Run `git commit --quiet --message …` alone in its command. Never `git stash`.

## Review Focus

1. **`--no-lakebase`.** Expected: no `session=` kwarg, and the effective session is the same as before (none declared, or the default). Pinned in Task 2.
2. **A base template (no catalog/schema).** Expected: no `catalog`/`schema`/`registered_model` in `[tool.apx.deploy]`, and no commented memory block. Pinned in Task 2.
3. **An `--instructions` override.** Expected: the agent's instructions are what the scaffold wrote before; nothing lost or doubled. Pinned in Task 2.
4. **A model-serving scaffold without a catalog** (no schema manifest). Expected: no `knowledge=`, and the project still imports. Pinned in Task 1.
5. **`scaffold --force` on a scaffolded `responses_agent` project.** Expected: the runtime is preserved (read statically from `agent.py`). Pinned in Task 2.

---

### Task 1: Model-serving scaffold

**Files:**
- Modify: `python/src/apx_agent/cli.py`: `_SCAFFOLD_PYPROJECT` (~1387), `_SCAFFOLD_AGENT` (~1415) and `_scaffold_model_serving` (~4052)
- Test: `python/tests/test_scaffold_templates.py` (create)

- [ ] **Step 1: Write the failing tests** (`python/tests/test_scaffold_templates.py`)

```python
"""Scaffold templates declare settings in agent.py (sub-project 3b-3c)."""

from __future__ import annotations

import runpy
import sys
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from apx_agent import cli
from apx_agent._inspection import _load_agent_config, static_agent_setting
from apx_agent._settings import resolve_settings


def _effective(root: Path):
    sys.path.insert(0, str(root))
    try:
        agent = runpy.run_path(str(root / "agent.py"))["agent"]
    finally:
        sys.path.remove(str(root))
    return agent, resolve_settings(agent, _load_agent_config(pyproject_path=root / "pyproject.toml"))


def test_model_serving_scaffold_has_no_agent_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_schema_manifest_for_scaffold", lambda *a, **k: None)
    root = tmp_path / "ms"
    cli._scaffold_model_serving(root, "ms", catalog="main", schema="sales", table=None)
    assert "agent" not in tomllib.loads((root / "pyproject.toml").read_text()).get("tool", {}).get("apx", {})
    agent, config = _effective(root)
    assert (config.name, config.model, config.description) == ("ms", "databricks-claude-sonnet-4-6", "An apx-agent.")
    assert "You are a helpful assistant." in agent._instructions  # overlaid, as the TOML did
    assert "main.sales" in agent._instructions  # the schema grounding is kept
    assert config.knowledge is None
```

Before running, check `_scaffold_model_serving`'s real signature (`sed -n "$(grep -n '^def _scaffold_model_serving' python/src/apx_agent/cli.py | cut -d: -f1),+12p" python/src/apx_agent/cli.py`) and adjust the call: the argument order, and whether `target` is a directory the function creates. Also check what `_schema_manifest_for_scaffold` returns offline. The goal is a no-manifest scaffold with no network.

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_scaffold_templates.py -q -p no:cacheprovider`
Expected: FAIL, because `[tool.apx.agent]` is still written.

- [ ] **Step 3: Implement.**
  - In `_SCAFFOLD_PYPROJECT`, delete the `[tool.apx.agent]` block (`name`, `description`, `model`, `instructions`, `{knowledge_line}`). Keep the comment about declaring tools in `tools/`.
  - In `_SCAFFOLD_AGENT`, change the constructor and add the overlay:

```python
agent = DataAgent("{catalog}", "{schema}", name="{name}", description="An apx-agent.",
                  model="databricks-claude-sonnet-4-6"{extra_tools}{knowledge_arg})
agent.overlay_instructions("You are a helpful assistant.")
```

  - In `_scaffold_model_serving`, drop the now-unused `knowledge_line` argument to `.format(...)`. Keep `knowledge_arg`.
  - `name` must stay a literal string in the template. The format substitutes `{name}`; escape any braces the template needs as `{{ }}`.

- [ ] **Step 4: Run to verify, and update affected tests**

Run: `cd python && uv run pytest tests/test_scaffold_templates.py tests/test_cli.py -q -p no:cacheprovider -k "scaffold or model_serving or templates"`
Expected: PASS. Any existing test that reads the model-serving `[tool.apx.agent]` moves to `_effective(root)` or `static_agent_setting`, keeping its intent. List each one in the report.

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/cli.py python/tests/test_scaffold_templates.py python/tests/
git commit --quiet --message "feat: model-serving scaffold declares settings in agent.py

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 2: Apps `responses_agent` scaffold

**Files:**
- Modify: `python/src/apx_agent/cli.py`:
  - `_SCAFFOLD_APPS_PYPROJECT` (~1483);
  - `_SCAFFOLD_APPS_AGENT` / `_SCAFFOLD_APPS_AGENT_BASE` / `_SCAFFOLD_APPS_AGENT_COWORKER` (~1545-1640);
  - `_SCAFFOLD_MEMORY_BLOCK` / `_SCAFFOLD_SESSION_LAKEBASE_BLOCK` (~1765-1790);
  - `_scaffold_apps` (~4156-4290).
- Test: `python/tests/test_scaffold_templates.py`; update `tests/test_scaffold_apps.py`, `tests/test_scaffold_reality_ctk.py` and `tests/test_cli.py` where they read `[tool.apx.agent]`.

- [ ] **Step 1: Write the failing tests** (append)

```python
@pytest.mark.parametrize("template", ["base", "data", "coworker"])
@pytest.mark.parametrize("lakebase", [True, False])
def test_apps_responses_scaffold_declares_settings_in_agent_py(tmp_path: Path, monkeypatch, template: str,
                                                               lakebase: bool) -> None:
    args = ["agents", "scaffold", "orders", "--here", "--dir", str(tmp_path), "--template", template,
            "--runtime", "responses_agent", "--no-interactive"] + ([] if lakebase else ["--no-lakebase"])
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 0, result.output
    root = tmp_path / "orders"
    data = tomllib.loads((root / "pyproject.toml").read_text())
    assert "agent" not in data["tool"]["apx"]
    assert static_agent_setting(root / "agent.py", "target") == "responses_agent"
    monkeypatch.chdir(root)
    agent, config = _effective(root)
    assert (config.name, config.target, config.model) == ("orders", "responses_agent", "databricks-claude-sonnet-4-6")
    assert config.description == "An apx-agent on Databricks Apps."
    assert config.examples == ["Show me a few sample rows from the data you can access",
                               "What questions can you answer?"]
    if lakebase:
        assert config.session is not None and config.session.type == "lakebase"
        assert config.session.database == "orders"
    else:
        assert "session=" not in (root / "agent.py").read_text()
    if template != "base":
        deploy = data["tool"]["apx"]["deploy"]
        assert {"catalog", "schema", "registered_model"} <= set(deploy)
        assert "# memory=" in (root / "agent.py").read_text()  # the commented memory hint moved with it
    else:
        assert "registered_model" not in data["tool"]["apx"].get("deploy", {})


def test_force_rescaffold_preserves_the_responses_runtime(tmp_path: Path) -> None:
    args = ["agents", "scaffold", "orders", "--here", "--dir", str(tmp_path), "--template", "base",
            "--no-interactive"]
    assert CliRunner().invoke(cli.main, args + ["--runtime", "responses_agent"]).exit_code == 0
    assert CliRunner().invoke(cli.main, args + ["--force"]).exit_code == 0
    assert static_agent_setting(tmp_path / "orders" / "agent.py", "target") == "responses_agent"
```

Before running, check:
- the scaffold's `--runtime` / `--no-lakebase` option names (`grep -n '"--no-lakebase"\|"--runtime"' python/src/apx_agent/cli.py | head`);
- whether `data`/`coworker` need a catalog/schema or profile offline. Copy the setup of `tests/test_scaffold_apps.py`'s existing responses-runtime tests for those templates, for example a monkeypatched `_schema_manifest_for_scaffold`.

Then capture the **before** values once on `main`: run these scaffolds in a scratch directory with the current `main` checkout and record each project's effective `name`, `model`, `target`, `description`, `examples` and `session`. The assertions above must match those. If any differs (for example `database` is the slug, not the name), use the real before-value and note it in the report.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_scaffold_templates.py -q -p no:cacheprovider -k "apps_responses or force_rescaffold"`
Expected: FAIL, because `[tool.apx.agent]` is still written.

- [ ] **Step 3: Implement.**
  - **`_SCAFFOLD_APPS_PYPROJECT`:**
    - delete the `[tool.apx.agent]` block (`name`, `target`, `description`, `model`, `<CATALOG_SCHEMA_CONFIG_BLOCK><REGISTERED_MODEL_CONFIG_BLOCK>`, `instructions`, `<KNOWLEDGE_LINE>`, `module`, `examples`);
    - put `<CATALOG_SCHEMA_CONFIG_BLOCK><REGISTERED_MODEL_CONFIG_BLOCK>` under a `[tool.apx.deploy]` header, emitted only when those blocks are non-empty. Do this in `_scaffold_apps`: build `deploy_block = "[tool.apx.deploy]\n" + catalog_schema_config_block + registered_model_config_block` when either is non-empty, else `""`. Replace both placeholders with one `<DEPLOY_BLOCK>`.
  - **The agent templates:** append `<SETTINGS_ARG>` right after the `name="<APP_NAME>"…` line's existing args, inside the constructor. In `_scaffold_apps`, set:

```python
    settings_arg = (
        ', target="responses_agent", model="databricks-claude-sonnet-4-6", '
        'description="An apx-agent on Databricks Apps.", '
        'examples=["Show me a few sample rows from the data you can access", "What questions can you answer?"]'
        + (f', session={{"type": "lakebase", "host": "${{LAKEBASE_HOST}}", "database": "{name_slug}"}}' if lakebase else "")
    )
```

  Only for the `responses_agent` runtime: the native (durable) path already injects its own settings (#996) and must not get these. Add `.replace("<SETTINGS_ARG>", settings_arg)` to `_sub`, with `settings_arg = ""` for the durable runtime.
  - **The default overlay:** the data and coworker templates' old TOML `instructions = "You are a helpful assistant."` overlaid their grounding. Append `agent.overlay_instructions("You are a helpful assistant.")` after their constructor, **only** when `instructions_arg` is empty. The base template already sets `instructions=<INSTRUCTIONS_VALUE>` in its constructor, so it gets no overlay.
  - **Sessions:** `_SCAFFOLD_SESSION_LAKEBASE_BLOCK` is no longer appended to the pyproject (the session is in `settings_arg`). Delete the constant if nothing else uses it.
  - **Memory:** replace the TOML `_SCAFFOLD_MEMORY_BLOCK` with a commented Python hint appended to `agent.py` when there's a catalog/schema:

```python
# Uncomment to enable persistent memory on Lakebase (set LAKEBASE_HOST; tables auto-create):
# memory={"type": "lakebase", "host": "${LAKEBASE_HOST}", "database": "<APP_NAME_SLUG>",
#         "table_name": "<CATALOG>.<SCHEMA>.apx_<APP_NAME_SLUG>_memory",
#         "embedding_model": "databricks-bge-large-en", "embedding_dim": 1024}
# — pass it as an argument to the agent constructor above.
```

- [ ] **Step 4: Run to verify, and update affected tests**

Run: `cd python && uv run pytest tests/test_scaffold_templates.py tests/test_scaffold_apps.py tests/test_scaffold_reality_ctk.py tests/test_scaffold_native.py tests/test_cli.py tests/test_cli_scaffold_yaml.py -q -p no:cacheprovider`
Expected: PASS. Existing tests that read `[tool.apx.agent]` from a scaffolded project move to `_effective(...)`, `static_agent_setting(...)` or `[tool.apx.deploy]`, keeping their intent. List each in the report.

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/cli.py python/tests/
git commit --quiet --message "feat: Apps responses scaffold declares settings in agent.py

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 3: Gate and changelog

- [ ] **Step 1: Gate.** `make check`; pre-commit on the changed files; `uv run pyright src/apx_agent/`; the PR CI subset under xdist, twice. Expected: all green.
- [ ] **Step 2: Changelog** under `### Added`:

```markdown
- **Scaffolds declare settings in Python.** The model-serving and Apps `responses_agent` scaffolds write their settings as keyword arguments on the agent in `agent.py` (the default Lakebase session included), and registration keys in `[tool.apx.deploy]`. No scaffold writes `[tool.apx.agent]` any more.
```

- [ ] **Step 3: Commit** (its own command)

```bash
git add CHANGELOG.md
git commit --quiet --message "docs: scaffolds declare settings in Python

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

## Out of this plan

- Migrating the in-repo projects (3c), and rejecting `[tool.apx.agent]` (3d).
