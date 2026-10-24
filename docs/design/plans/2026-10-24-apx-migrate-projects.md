# Sub-project 3c: migrate the in-repo projects off `[tool.apx.agent]` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every in-repo project except the `examples/graph_spec` YAML-spec input declares its settings in Python and `[tool.apx.deploy]`, with no `[tool.apx.agent]` table. Each agent that could be served on `main` serves the same thing after the change.

**Architecture:**
- **Migration tool.** `apx-agent agents migrate-config` does each move. It moves deploy fields itself, and it removes the table only once the Python agent declares every runtime field. It also restores the file if the effective config changes.
- **This plan adds:**
  - a fix that lets migrate-config count an `overlay_instructions(...)` line as declaring `instructions`;
  - a guard test plus a characterization test pinned to what `main` serves;
  - per-project edits to `agent.py`, so migrate-config can finish.
- **Projects that fail on `main` are repaired:** the 8 `samples-*` and `payroll-coworker`.

**Tech Stack:** Python ≥3.10, pytest, ctk.

**Spec:** `docs/design/agent-toml-source-of-truth.md`, section "The four sub-projects", item 3c ("Migrate the in-repo projects with `migrate-config`").

## Global Constraints

- **Running tests and the gate:**
  - Run tests from `python/`: `cd python && uv run pytest … -q -p no:cacheprovider`.
  - Gate: `make check`; `pre-commit run --files <changed files>`; `uv run pyright src/apx_agent/`; the PR CI subset under xdist. In zsh: ``paths=$(BASE=origin/main HEAD=HEAD EVENT_NAME=pull_request PR_PYTEST_CHANGED_FILES="$(git diff --name-only origin/main...HEAD; git diff --name-only)" bash ../scripts/pr-pytest-paths.sh); uv run pytest ${=paths} -n auto --timeout=90 --timeout-method=thread``.
- **Lint bans:**
  - `.get(k, "")` and `x or ""`;
  - `getattr(x, "a", "<str literal>")`;
  - `object` annotations;
  - `str = ""` defaults;
  - invented env defaults;
  - `tuple[...]` returns;
  - unconditional `@pytest.mark.skip`.
- **No served-behavior change** for projects that load on `main`. The characterization test (Task 2) is the proof: name, model, target, description, examples, knowledge, session, memory, `max_iterations`, and the sha256 of the finalized root instructions.
- **migrate-config does the move.** Hand edits are limited to:
  - adding keyword arguments or an `overlay_instructions(...)` line to `agent.py`;
  - deleting TOML keys that never had an effect.

  Each deletion gets a `Ruling:` with the evidence.
- **migrate-config refuses a dirty `pyproject.toml`.** For each project:
  1. Commit the hand edits.
  2. Run migrate-config.
  3. Commit its result.

  Never `git stash`.
- **Commits:**
  - end with `Co-authored-by: Isaac <no-reply@databricks.com>`;
  - run `git commit --quiet --message …` alone in its own command;
  - `uv.lock` files: `grep -c 'pypi-proxy' <lock>` must be 0 before staging.
- **Public repo:** no roadmap in commits or the PR.

## Survey (measured 2026-10-24 on 68635e21)

`migrate-config` was dry-run on a git-initialised copy of each project. Results by group:

| Group | Projects | Blocker | Fix |
|---|---|---|---|
| A — importable | `python/examples/`: `bakehouse-agent`, `customer_triage`, `customer_triage_fleet/{account_specialist,billing_specialist,orchestrator,technical_specialist}`, `eligibility-agent`, `explain-my-bill-agent`, `hubspot-complaints-agent`, `memory_demo`, `pe55-support-agent`, `plg-discovery`, `sec-10k-agent`, `shortage-intelligence-agent`, `slack-agent`, `zero-ops-diagnostics`; plus `python/hello-world` (17) | runtime fields only in TOML (`description`, `model`, sometimes `instructions`, `max_iterations`, `examples`, `knowledge`, empty `url`/`registry`) | Task 3 |
| B — vendored dep | `contract-parsing-agent`, `data-inspector`, `data-triage-agent`, `entity-resolution-agent` (4) | `agent.py` imports `databricks_tools_core`, not in apx's venv | Task 4; run with `uv run --with python/examples/data-inspector/databricks_tools_core-0.1.0-py3-none-any.whl` |
| D — broken on `main` | `python/samples-{accuweather,bakehouse,healthverity,hub,tpcds-sf1,tpcds-sf1000,tpch,wanderbricks}` (8) | Lakebase session with no `target`. These are `compile_to_responses_agent` apps, but the default target is now durable, so the config doesn't validate | Task 5; add `target="responses_agent"` |
| E — removed backend | `python/payroll-coworker` | `memory`/`session` `type = "delta"`; the Delta backend is gone | Task 6 |
| C — no `agent.py` | `python/examples/apx-builder` (`module = "app:agent"`) | migrate-config rejects any module other than `agent:agent` | Task 6, by hand |
| excluded | `examples/graph_spec` | A YAML graph spec (input to `generate_project`, read by `test_example_graph_spec_compiles`), not a project | Out of this plan |

Facts the tasks rely on:

- **Empty `url` and `registry`.** `url = ""` / `registry = ""` have no effect. The only reader is `_wiring.py:894` (`if config.registry:` / `if config.url`).
- **TOML `instructions` on a composite root.** On a `RouterAgent` root (`bakehouse-agent`, `zero-ops-diagnostics`), TOML `instructions` had no effect. `apply_config_knobs` skips roots without `_instructions` (`_wiring.py`, the "Composition roots … Skipping is intentional" branch). Task 2's snapshot confirms it.
- **TOML `instructions` on an `LlmAgent` root.** These were overlaid above the agent's own instructions at finalize. The faithful migration is `agent.overlay_instructions("<text>")` after the constructor (as in #996/#997). migrate-config's `_covered` compares `agent._instructions` for equality, so it never counts an overlay; Task 1 fixes that.

## Review Focus

1. **`instructions` declared twice, as a constructor kwarg and as an overlay.** After Task 1, `_covered` must count the overlay only when its text equals the TOML value. A different overlay text still counts as missing. Pinned in Task 1.
2. **A project whose finalize differs only in the instructions hash.** That means a doubled or missing overlay. The characterization test compares the hash, not a prefix. Pinned in Task 2.
3. **Sibling modules shared across projects** (`tools/`, `config.py`). Snapshots run each project in its own subprocess, so one project's `tools` package can't shadow another's. Pinned in Task 2.
4. **Re-running migrate-config on a migrated project.** Expected: "nothing to migrate" and no diff. Pinned in Task 7.
5. **A new in-repo project that brings `[tool.apx.agent]` back.** The guard test fails and names the file. Pinned in Task 2.

---

### Task 1: migrate-config counts an instructions overlay

**Files:**
- Modify: `python/src/apx_agent/_agents.py`, `LlmAgent.overlay_instructions` (~503)
- Modify: `python/src/apx_agent/_migrate_config.py`, `_covered`
- Test: `python/tests/test_migrate_config.py`

**Interfaces:**
- Produces: `LlmAgent._persona_overlay: str | None`, the overlay text that `overlay_instructions` applied, or `None`. `_covered("instructions", value, …)` is True when `agent._instructions == value` or `agent._persona_overlay == value`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_migrate_config.py`)

```python
def test_an_instructions_overlay_counts_as_declared(tmp_path: Path) -> None:
    path = _write(tmp_path, '[project]\nname = "p"\n\n[tool.apx.agent]\nname = "p"\n'
                            'instructions = "You are a helpful assistant."\n')
    agent = Agent(name="p", instructions="Grounded in main.sales.")
    agent.overlay_instructions("You are a helpful assistant.")
    migration = plan_migration(path, agent)
    assert migration.removed_agent_table, migration.missing


def test_a_different_overlay_is_still_missing(tmp_path: Path) -> None:
    path = _write(tmp_path, '[project]\nname = "p"\n\n[tool.apx.agent]\nname = "p"\n'
                            'instructions = "You are a helpful assistant."\n')
    agent = Agent(name="p", instructions="Grounded.")
    agent.overlay_instructions("Be terse.")
    assert plan_migration(path, agent).missing == {"instructions": "You are a helpful assistant."}
```

Before running, check how `_write` composes its text (`sed -n 1,40p tests/test_migrate_config.py`). Match its pyproject shape. If it needs the `[project]` table it already writes, drop the duplicate here.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_migrate_config.py -q -p no:cacheprovider -k "overlay"`
Expected: the first test FAILS (`removed_agent_table` is False and `missing` holds `instructions`). The second PASSES already; it guards against a too-loose fix.

- [ ] **Step 3: Implement.**
  - In `LlmAgent.overlay_instructions`, record the text inside the existing `if`:

```python
        if overlay.strip() and not getattr(self, "_persona_overlaid", False):
            self._instructions = compose_instructions(base=self._instructions, overlay=overlay)
            self._persona_overlaid = True
            self._persona_overlay = overlay
```

  - In `_covered`, before the `_CONSTRUCTOR` branch:

```python
    if field == "instructions" and getattr(agent, "_persona_overlay", None) == value:
        return True  # overlay_instructions(...) is how agent.py declares a TOML persona (#996)
```

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_migrate_config.py tests/test_scaffold_templates.py tests/test_wiring.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/_agents.py python/src/apx_agent/_migrate_config.py python/tests/test_migrate_config.py
git commit --quiet --message "fix: migrate-config counts an overlay_instructions persona as declared

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 2: Guard and characterization tests

**Files:**
- Create: `python/tests/test_in_repo_projects.py`
- Create: `python/tests/fixtures/in_repo_projects_main.json` (captured from `main`, before any project edit)

**Interfaces:**
- Produces:
  - `_SERVED`: a module constant, the subprocess script;
  - `_served(root: Path) -> dict[str, Any]`;
  - `_MIGRATED: dict[str, str]`, a project dir relative to the repo root mapped to its deploy name. It starts **empty**, and Tasks 3–6 add their projects.

- [ ] **Step 1: Write the test file**

```python
"""In-repo projects declare settings in Python, not [tool.apx.agent] (sub-project 3c)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from apx_agent._inspection import _load_agent_config

_REPO = Path(__file__).resolve().parents[2]
_MAIN = json.loads((Path(__file__).parent / "fixtures" / "in_repo_projects_main.json").read_text())
_NOT_A_PROJECT = {"examples/graph_spec"}  # a YAML graph spec fed to generate_project (sub-project 4)
_MIGRATED: dict[str, str] = {}

# Import and finalize one project in a fresh interpreter (its tools/ and config.py never
# shadow another project's) and print what it would serve.
_SERVED = r'''
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1]); sys.path.insert(0, str(root))
import runpy
from apx_agent._inspection import _load_agent_config
from apx_agent._settings import resolve_settings
from apx_agent._wiring import finalize_agent
agent = runpy.run_path(str(root / "agent.py"))["agent"]
config = resolve_settings(agent, _load_agent_config(pyproject_path=root / "pyproject.toml"))
finalize_agent(agent, config, pyproject_path=str(root / "pyproject.toml"))
instructions = getattr(agent, "_instructions", None)
dump = lambda m: None if m is None else m.model_dump(mode="json")
print(json.dumps({
    "name": config.name, "model": config.model, "target": config.target,
    "description": config.description, "examples": config.examples, "knowledge": config.knowledge,
    "session": dump(config.session), "memory": dump(config.memory),
    "max_iterations": getattr(agent, "_max_iterations", None),
    "instructions_sha256": None if instructions is None else hashlib.sha256(instructions.encode()).hexdigest(),
}))
'''


def _served(root: Path) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("DATABRICKS_")}  # no workspace
    out = subprocess.run([sys.executable, "-c", _SERVED, str(root)], cwd=root, env=env,
                         capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_no_project_declares_the_agent_table() -> None:
    offenders = sorted(
        str(p.parent.relative_to(_REPO)) for p in _REPO.glob("**/pyproject.toml")
        if ".venv" not in p.parts and "tests" not in p.parts
        and str(p.parent.relative_to(_REPO)) not in _NOT_A_PROJECT
        and "agent" in tomllib.loads(p.read_text()).get("tool", {}).get("apx", {}))
    assert offenders == [], "run `apx-agent agents migrate-config` in: " + ", ".join(offenders)


@pytest.mark.parametrize("project", sorted(_MIGRATED))
def test_migrated_project_loads_with_its_name(project: str) -> None:
    pyproject = _REPO / project / "pyproject.toml"
    assert "agent" not in tomllib.loads(pyproject.read_text()).get("tool", {}).get("apx", {})
    config = _load_agent_config(pyproject_path=pyproject)
    assert config is not None and config.name == _MIGRATED[project]


@pytest.mark.parametrize("project", sorted(_MAIN))
def test_project_serves_what_main_served(project: str) -> None:
    assert _served(_REPO / project) == _MAIN[project]
```

Check the glob's exclusions against the tree (`git ls-files '**/pyproject.toml'`), and exclude anything git-ignored or vendored. `test_migrated_project_loads_with_its_name` has no cases until Task 3. Pytest reports an empty parametrize as one skipped case, not a decorator skip, and the lint ban targets only unconditional `@pytest.mark.skip`.

- [ ] **Step 2: Capture `main`'s snapshot.** Nothing in any project has changed yet, and Task 1 doesn't change finalize output. Run `_served` for every group-A project and write the fixture:

```bash
cd python && uv run python - <<'EOF'
import json, sys; sys.path.insert(0, "tests")
from pathlib import Path
from test_in_repo_projects import _REPO, _served
group_a = ["python/examples/" + p for p in (
    "bakehouse-agent", "customer_triage", "customer_triage_fleet/account_specialist",
    "customer_triage_fleet/billing_specialist", "customer_triage_fleet/orchestrator",
    "customer_triage_fleet/technical_specialist", "eligibility-agent", "explain-my-bill-agent",
    "hubspot-complaints-agent", "memory_demo", "pe55-support-agent", "plg-discovery", "sec-10k-agent",
    "shortage-intelligence-agent", "slack-agent", "zero-ops-diagnostics")] + ["python/hello-world"]
snap = {p: _served(_REPO / p) for p in group_a}
Path("tests/fixtures/in_repo_projects_main.json").write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
print(len(snap))
EOF
```

The fixture file must exist before the module imports. Create it as `{}` first, then run the capture.

Expected: `17`. If a project fails to finalize offline, leave it out of the fixture: it's covered only by the static load test. Record `Task 2: Ruling: <project> finalize needs a workspace — static-only`.

Group B can't import in the test venv (`databricks_tools_core`). Capture its snapshot in a scratch file with `uv run --with <wheel> python …`, and compare by hand in Task 4. The test file doesn't include it.

- [ ] **Step 3: Run to verify**

Run: `cd python && uv run pytest tests/test_in_repo_projects.py -q -p no:cacheprovider`
Expected:
- `test_no_project_declares_the_agent_table` FAILS and lists the 31 projects;
- the 17 `test_project_serves_what_main_served` cases PASS;
- `test_migrated_project_loads_with_its_name` has no cases.

The guard stays red until Task 7. Commit with it red, and say so in the message; the branch is green only at Task 7.

Check ctk-style that the fixture is real: `ctk.verify(Artifact(fixture, min_bytes=2000, must_contain=['"instructions_sha256"', "hello-world"]))`.

- [ ] **Step 4: Commit** (its own command)

```bash
git add python/tests/test_in_repo_projects.py python/tests/fixtures/in_repo_projects_main.json
git commit --quiet --message "test: pin what in-repo projects serve on main; guard against [tool.apx.agent]

The guard fails until every project is migrated (later commits in this branch).

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 3: Migrate group A (16 examples + hello-world)

**Files:**
- Modify: `agent.py` and `pyproject.toml` in each group-A project
- Modify: `python/tests/test_in_repo_projects.py` (`_MIGRATED`)

For each project, in this order:

- [ ] **Step 1: Add the group to `_MIGRATED`** (project dir → old `[tool.apx.agent].name`):

```bash
cd /path/to/worktree && for p in <group A dirs>; do python3 -c "import tomllib,sys; print(repr(sys.argv[1]), ':', repr(tomllib.load(open(sys.argv[1]+'/pyproject.toml','rb'))['tool']['apx']['agent']['name']), ',')" $p; done
```

Paste the output into `_MIGRATED`. Run `uv run pytest tests/test_in_repo_projects.py -q -p no:cacheprovider -k loads_with_its_name`.
Expected: all 17 FAIL on `"agent" not in …`.

- [ ] **Step 2: Hand edits per project.** Run `apx-agent agents migrate-config` in a scratch copy, or read the survey, to get the missing fields. Then:
  - **Constructor kwargs.** Add `description=`, `model=`, `max_iterations=`, `examples=` and `knowledge=` as literal kwargs on the root `agent = …(...)` constructor, with values copied verbatim from the TOML.
  - **`instructions` on an `LlmAgent` root.** If the root is an `LlmAgent` (`Agent`, `DataAgent`, `CoworkerAgent`) and the TOML has `instructions`, add `agent.overlay_instructions("<TOML instructions>")` right after the constructor. Don't pass it as `instructions=`: finalize composes it above the agent's own prompt.
  - **`instructions` on a composite root.** If the root is composite (`RouterAgent`: `bakehouse-agent`, `zero-ops-diagnostics`) and the TOML has `instructions`, delete the TOML line. Ledger `Ruling: <project> TOML instructions had no effect on a RouterAgent root (apply_config_knobs skips composites) — deleted — cost if wrong: the snapshot test fails`.
  - **Empty `url`/`registry`.** Delete `url = ""` / `registry = ""` lines (`shortage-intelligence-agent`). Ledger the ruling with `_wiring.py:894` as evidence.
  - **Root not named `agent`.** If a project's root isn't a top-level `agent = …` constructor call (a factory, or a variable built elsewhere), stop and ledger a ruling: put the kwargs where the root object is constructed, keeping them literal.

- [ ] **Step 3: Verify served behavior, then commit the hand edits**

Run: `cd python && uv run pytest tests/test_in_repo_projects.py -q -p no:cacheprovider -k serves_what_main_served`
Expected: all 17 PASS. A hash mismatch means an overlay is doubled or missing: fix `agent.py`, never the fixture.

```bash
git add <group A agent.py and pyproject.toml files>
git commit --quiet --message "refactor: group-A examples declare their settings on the agent

Co-authored-by: Isaac <no-reply@databricks.com>"
```

- [ ] **Step 4: Run migrate-config in each project**

```bash
for p in <group A dirs>; do (cd $p && uv run --project <worktree>/python apx-agent agents migrate-config) || echo "FAILED $p"; done
```

Expected for each: `removed [tool.apx.agent]: every field is declared in Python`. A `kept [tool.apx.agent]` means a field is still missing. Add it, amend the hand-edit commit (it isn't pushed), and re-run. Read back one migrated `pyproject.toml` and confirm `[tool.apx.deploy]` carries `name` and any registration keys. Check that `.apx.local` files migrated or are absent; they're gitignored.

- [ ] **Step 5: Run to verify**

Run: `cd python && uv run pytest tests/test_in_repo_projects.py tests/test_shortage_intel_parity.py tests/test_pe55_support_agent_example.py tests/test_sec_10k_agent_example.py tests/test_apps_discovery.py tests/test_effective_config.py tests/test_topology.py tests/test_deploy_table.py -q -p no:cacheprovider`
Expected:
- `loads_with_its_name` and `serves_what_main_served` PASS for all 17;
- the guard lists only groups B–E;
- the example tests PASS.

If an example test reads the old table, move it to the new home and keep its intent. List each one in the ledger.

- [ ] **Step 6: Update READMEs.** Run `grep -ln 'tool.apx.agent' <group A dirs>/README.md`. In each hit, replace the `[tool.apx.agent]` snippet with the agent.py kwargs / `[tool.apx.deploy]` it now uses.

- [ ] **Step 7: Commit** (its own command)

```bash
git add <group A pyproject.toml files> <READMEs> python/tests/
git commit --quiet --message "refactor: migrate group-A examples off [tool.apx.agent] with migrate-config

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 4: Migrate group B (vendored `databricks_tools_core`)

**Files:** `agent.py` and `pyproject.toml` in `contract-parsing-agent`, `data-inspector`, `data-triage-agent` and `entity-resolution-agent`; `_MIGRATED`.

- [ ] **Step 1: Add the four projects to `_MIGRATED`.** Run the `loads_with_its_name` cases. Expected: FAIL.
- [ ] **Step 2: Snapshot before the edits.** In a scratch file (not committed), run `_SERVED` for each project under `uv run --project python --with python/examples/data-inspector/databricks_tools_core-0.1.0-py3-none-any.whl python -c "$SERVED" <dir>`. Save the four JSON lines to `/tmp/mig3c/groupB_main.json`.
- [ ] **Step 3: Hand edits.**
  - Add `description=` and `model=` kwargs on each root.
  - `entity-resolution-agent` also gets `url="$ENTITY_RESOLUTION_AGENT_URL"` and `registry="$AGENT_HUB_URL"` as kwargs (they're `APP_WIDE` settings).
  - Delete the empty `url = ""` / `registry = ""` lines in the other three (same ruling as Task 3).
  - Commit the hand edits.
- [ ] **Step 4: Run migrate-config** in each, with the wheel: `uv run --project <worktree>/python --with <wheel> apx-agent agents migrate-config`. Expected: `removed [tool.apx.agent]` for each.
- [ ] **Step 5: Verify.**
  - Re-run Step 2's snapshot into `/tmp/mig3c/groupB_after.json`, then `diff` the two files. Expected: identical.
  - `uv run pytest tests/test_in_repo_projects.py -q -p no:cacheprovider -k loads_with_its_name`. Expected: PASS for all 21.
  - Update READMEs as in Task 3, Step 6.
- [ ] **Step 6: Commit** (its own command): `refactor: migrate the data-tools examples off [tool.apx.agent]`, with the Isaac trailer.

---

### Task 5: Repair and migrate the `samples-*` apps (broken on `main`)

**Files:** `agent.py` and `pyproject.toml` in the 8 `python/samples-*` projects; `_MIGRATED`.

- [ ] **Step 1: Add the 8 projects to `_MIGRATED`** (deploy name = the old TOML `name`). Run `loads_with_its_name` for them. Expected: FAIL. Their `_load_agent_config` raises `Declared durable sessions require type='managed' (got 'lakebase')`, which is the bug on `main`.
- [ ] **Step 2: Hand edits.** Each `agent_server/start_server.py` calls `compile_to_responses_agent`, so these are `responses_agent` apps. Mirror the #997 scaffold output exactly. On the root `agent = DataAgent(...)` add:
  - `target="responses_agent"`, `description=`, `model=`, `knowledge=` and `examples=`, copied verbatim from the TOML;
  - `session={"type": "lakebase", "host": "${LAKEBASE_HOST}", "database": "<the TOML database>"}`.

  After the constructor, add `agent.overlay_instructions("You are a helpful assistant.")`. Turn the commented `[tool.apx.agent.memory]` block into the commented `memory={...}` hint that #997's `_SCAFFOLD_MEMORY_HINT` writes, with the same table name. Commit the hand edits.
- [ ] **Step 3: Run migrate-config** in each. Expected: `removed [tool.apx.agent]`.
- [ ] **Step 4: Verify**
  - `uv run pytest tests/test_in_repo_projects.py -q -p no:cacheprovider -k loads_with_its_name`. Expected: PASS for all 29.
  - Prove each one serves: run `_served(<samples dir>)` once per project in a scratch shell. Expected: `target == "responses_agent"`, `session["type"] == "lakebase"`, the `instructions_sha256` of `"You are a helpful assistant.\n\n---\n\n" + <grounding>`, and no exception.
  - Then add the 8 projects' `_served` output to `in_repo_projects_main.json` under a `# repaired` note in the commit message. They had no working `main` state, so this pins the repaired state. Ledger `Task 5: Ruling: samples-* pinned at their repaired state (no loadable main state) — cost if wrong: a wrong repair is pinned`.
- [ ] **Step 5: Commit** (its own command): `fix: samples-* apps declare target="responses_agent" and migrate off [tool.apx.agent]`, with the Isaac trailer.

---

### Task 6: `payroll-coworker` and `apx-builder` (by hand)

**Files:** `python/payroll-coworker/{agent.py,pyproject.toml}`, `python/examples/apx-builder/{app.py,pyproject.toml}`; `_MIGRATED`.

- [ ] **Step 1: Add both to `_MIGRATED`** (`"python/payroll-coworker": "payroll-coworker"`, `"python/examples/apx-builder": "apx_builder"`). Expected: FAIL.
- [ ] **Step 2: `payroll-coworker`.**
  - Delete the `[tool.apx.agent.memory]` and `[tool.apx.agent.session]` `type = "delta"` blocks. The Delta backend no longer exists, and both tables were in a workspace that's gone. The coworker then runs on its defaults: memory off (#839) and the default session.
  - Add `description=`, `model=` and `examples=` kwargs, and `agent.overlay_instructions("<TOML instructions>")` after the constructor.
  - Ledger `Task 6: Ruling: payroll-coworker drops its removed Delta memory/session (no live backend or workspace) — cost if wrong: the demo stops remembering until a Lakebase/managed store is declared`.
  - Commit, then run migrate-config. Expected: `removed [tool.apx.agent]`.
- [ ] **Step 3: `apx-builder`.**
  - migrate-config refuses `module = "app:agent"`, so migrate by hand. Add `name="apx_builder"`, `description=` and `model=` to `app.py`'s `agent = Agent(...)`; check first whether `name` is already set.
  - Replace the `[tool.apx.agent]` table with `[tool.apx.deploy]\nname = "apx_builder"`. Drop `url = ""` (no effect) and `module =` (nothing reads it).
  - Keep `[tool.apx.metadata]`.
  - Verify that `_load_agent_config(pyproject_path=…)` gives `name == "apx_builder"`, and that `cd python/examples/apx-builder && uv run --project ../../ python -c "import app"` still imports, if it imported on `main`; check `main` first.
- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_in_repo_projects.py -q -p no:cacheprovider`
Expected: everything PASSES, including `test_no_project_declares_the_agent_table`.
- [ ] **Step 5: Commit** (its own command): `fix: payroll-coworker and apx-builder migrate off [tool.apx.agent]`, with the Isaac trailer.

---

### Task 7: Gate, re-run check, changelog

- [ ] **Step 1: Check that a re-run is a no-op.** Run migrate-config again in every migrated project except `apx-builder` (with the wheel for group B). Expected: `nothing to migrate` for each, and `git status --porcelain` empty.
- [ ] **Step 2: Gate.** Run `make check`, pre-commit on the changed files, `uv run pyright src/apx_agent/`, and the PR CI subset under xdist, twice. Expected: all green. Check every touched `uv.lock` has 0 `pypi-proxy` lines; the diff shouldn't touch any lock.
- [ ] **Step 3: Changelog** under `### Changed`:

```markdown
- **In-repo projects declare settings in Python.** The examples, `hello-world`, the `samples-*` apps and `payroll-coworker` no longer use `[tool.apx.agent]`; they were migrated with `apx-agent agents migrate-config`. The `samples-*` apps declare `target="responses_agent"`, which fixes their Lakebase session config, and `payroll-coworker` drops its removed Delta stores. `migrate-config` now counts an `agent.overlay_instructions(...)` line as declaring `instructions`.
```

- [ ] **Step 4: Commit** (its own command): `docs: in-repo projects declare settings in Python`, with the Isaac trailer.

---

## Out of this plan

- `examples/graph_spec`: a YAML graph spec that `generate_project` reads from `[tool.apx.agent]`. Its home is the YAML spec path (sub-project 4, #152). 3d has to keep accepting it there, or move it first.
- Docs outside the migrated projects' READMEs that show `[tool.apx.agent]` (3d's error message points at migrate-config, and the docs sweep goes with it).
- Making `[tool.apx.agent]` an error and removing `template =` / `module =` (3d).
