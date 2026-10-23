# Sub-project 3b-3a: memory and session backends on the agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `LlmAgent(memory=…)` also accepts a `MemoryBackendConfig` or a dict, and a new `session=` accepts a `SessionBackendConfig` or a dict. Explicit Lakebase backends then live in Python.

**Architecture:** Only `LlmAgent.__init__` changes. A string `memory=` still goes through `normalize_memory_knob`. A model or dict is validated with the existing models. `session=` replaces the knob's session. The results land in the existing `memory_config` / `session_config`, which `resolve_settings` already reads, so `migrate-config` and every reader work unchanged.

**Tech Stack:** Python ≥3.10, pydantic v2, pytest.

**Spec:** `docs/design/agent-toml-source-of-truth.md`, section "Sub-project 3b-3a: memory and session backends on the agent".

## Global Constraints

- **Running tests and the gate:**
  - Run tests from `python/`: `cd python && uv run pytest … -q -p no:cacheprovider`.
  - Gate: `make check`; `pre-commit run --files <changed files>`; the PR CI subset under xdist. In zsh: ``paths=$(BASE=origin/main HEAD=HEAD EVENT_NAME=pull_request PR_PYTEST_CHANGED_FILES="$(git diff --name-only origin/main...HEAD; git diff --name-only)" bash ../scripts/pr-pytest-paths.sh); uv run pytest ${=paths} -n auto --timeout=90 --timeout-method=thread``.
- **Lint bans:**
  - `.get(k, "")` and `x or ""`;
  - `getattr(x, "a", "<str literal>")`;
  - `object` annotations;
  - `str = ""` defaults;
  - invented env defaults;
  - `tuple[...]` returns;
  - unconditional `@pytest.mark.skip`.
- **No new classes.** Reuse `MemoryBackendConfig` and `SessionBackendConfig`; both are already exported from `apx_agent`.
- **Commits** end with `Co-authored-by: Isaac <no-reply@databricks.com>`. Run `git commit --quiet --message …` alone in its command. Never `git stash`.

## Review Focus

1. **A dict with an unknown key** (a typo such as `hots=`). Expected: construction fails with the model's own validation error (the models are `extra="forbid"`), not a silent accept. Pinned in Task 1.
2. **`memory="managed"` together with an explicit `session=`.** Expected: the managed memory stays, and the session is the explicit one. Pinned in Task 1.
3. **`memory=<object>` with no `session=`.** Expected: no session is invented; `session_config` is `None`, which is the TOML's job to fill. Pinned in Task 1.
4. **A Python-declared Lakebase session and a TOML session.** Expected: the TOML wins the gap, as #984 decided for memory/session. Pinned in Task 1.
5. **`DataAgent`/`CoworkerAgent` passing `memory` and `session` through `**kwargs`.** Expected: same behavior as `LlmAgent`. Pinned in Task 1.

---

### Task 1: `LlmAgent` takes backend objects

**Files:**
- Modify: `python/src/apx_agent/_agents.py` (`LlmAgent.__init__`: the `memory: str = "off"` parameter ~line 236 and the `normalize_memory_knob` call ~line 327)
- Test: `python/tests/test_memory_objects.py` (create)

**Interfaces:**
- Produces: `LlmAgent(..., memory: str | MemoryBackendConfig | Mapping[str, Any] = "off", session: SessionBackendConfig | Mapping[str, Any] | None = None)`.

- [ ] **Step 1: Write the failing tests** (`python/tests/test_memory_objects.py`)

```python
"""memory= / session= accept backend objects (sub-project 3b-3a)."""

from __future__ import annotations

import pydantic
import pytest

from apx_agent import Agent, DataAgent, MemoryBackendConfig, SessionBackendConfig
from apx_agent._models import AgentConfig
from apx_agent._settings import resolve_settings

_SESSION = {"type": "lakebase", "host": "${LAKEBASE_HOST}", "database": "orders"}
_MEMORY = {"type": "lakebase", "host": "${LAKEBASE_HOST}", "database": "orders",
           "table_name": "main.orders.apx_memory", "embedding_model": "databricks-bge-large-en",
           "embedding_dim": 1024}


def test_session_object_and_dict_give_the_same_config() -> None:
    by_model = Agent(name="o", tools=[], session=SessionBackendConfig(**_SESSION))
    by_dict = Agent(name="o", tools=[], session=_SESSION)
    assert by_model.session_config == by_dict.session_config == SessionBackendConfig(**_SESSION)


def test_memory_object_and_dict_give_the_same_config() -> None:
    assert (Agent(name="o", tools=[], memory=_MEMORY).memory_config
            == Agent(name="o", tools=[], memory=MemoryBackendConfig(**_MEMORY)).memory_config
            == MemoryBackendConfig(**_MEMORY))


def test_an_object_memory_invents_no_session() -> None:
    assert Agent(name="o", tools=[], memory=_MEMORY).session_config is None


def test_session_replaces_the_knobs_session_only() -> None:
    knob_only = Agent(name="o", tools=[], memory="managed")
    agent = Agent(name="o", tools=[], memory="managed", session=_SESSION)
    assert agent.memory_config == knob_only.memory_config
    assert agent.session_config == SessionBackendConfig(**_SESSION)


def test_a_typo_in_a_backend_dict_fails_at_construction() -> None:
    with pytest.raises(pydantic.ValidationError):
        Agent(name="o", tools=[], session={**_SESSION, "hots": "x"})


def test_python_declared_session_reaches_the_effective_config() -> None:
    config = resolve_settings(Agent(name="o", tools=[], session=_SESSION), None)
    assert config.session == SessionBackendConfig(**_SESSION)


def test_the_toml_session_wins_the_gap() -> None:
    toml = AgentConfig.model_validate({"name": "o", "session": {"type": "lakebase", "host": "h", "database": "toml"}})
    config = resolve_settings(Agent(name="o", tools=[], session=_SESSION), toml)
    assert config.session is not None and config.session.database == "toml"


def test_data_agent_passes_backends_through() -> None:
    agent = DataAgent("main", "orders", name="d", session=_SESSION)
    assert agent.session_config == SessionBackendConfig(**_SESSION)
```

Check the validation details before running:
- `SessionBackendConfig(type="lakebase", host=…, database=…)` must validate standalone. Probe it with `cd python && uv run python -c "from apx_agent import SessionBackendConfig as S; print(S(type='lakebase', host='h', database='d'))"`.
- The models must forbid extra keys, which the typo test relies on (`grep -n "class SessionBackendConfig\|class MemoryBackendConfig\|class _BackendConfig" -A4 python/src/apx_agent/_models.py`). If they don't forbid extras, the typo test must instead assert the documented behavior, and you ledger that as a ruling. The spec relies on the models' validation, and doesn't add any of its own.
- If `DataAgent(...)` needs a warehouse or workspace at construction, copy how `tests/test_data_agent*.py` constructs one.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_memory_objects.py -q -p no:cacheprovider`
Expected: FAIL. `session=` is an unknown keyword (`TypeError: unknown agent setting(s): session`, or an unexpected keyword argument), and `memory=<dict>` fails in `normalize_memory_knob`.

- [ ] **Step 3: Implement.** In `LlmAgent.__init__`:
  - change `memory: str = "off",` to `memory: "str | MemoryBackendConfig | Mapping[str, Any]" = "off",`;
  - add `session: "SessionBackendConfig | Mapping[str, Any] | None" = None,` directly after it;
  - replace the `normalize_memory_knob` assignment with:

```python
        if isinstance(memory, str):
            memory_config, session_config = normalize_memory_knob(
                memory,
                catalog=getattr(self, "catalog", None),
                schema=getattr(self, "schema", None),
                name=self._name,
            )
        else:
            # Explicit backend (3b-3a): the models validate it; no session is invented.
            memory_config, session_config = MemoryBackendConfig.model_validate(memory), None
        if session is not None:
            session_config = SessionBackendConfig.model_validate(session)
        self.memory_config, self.session_config = memory_config, session_config
```

Keep the existing two annotation lines (`self.memory_config: MemoryBackendConfig | None` / `self.session_config: SessionBackendConfig | None`) above it. `MemoryBackendConfig`, `SessionBackendConfig` and `Mapping` are already imported in `_agents.py` (check with `grep -n "MemoryBackendConfig\|SessionBackendConfig\|Mapping" python/src/apx_agent/_agents.py | head -5`). Add any that are missing to the existing import lists.

Also update the `memory` parameter's docstring line, if `LlmAgent`'s docstring documents it, to mention that a `MemoryBackendConfig` or dict is accepted and to document `session=`.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_memory_objects.py tests/test_agents.py tests/test_agent_settings.py tests/test_resolve_settings.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit** (its own command)

```bash
git add python/src/apx_agent/_agents.py python/tests/test_memory_objects.py
git commit --quiet --message "feat: memory= and session= accept backend objects on the agent

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 2: `migrate-config` moves a Lakebase session; docs

**Files:**
- Test: `python/tests/test_migrate_config.py`
- Modify: `python/src/apx_agent/_models.py` (the `normalize_memory_knob` "lakebase" error text)
- Modify: `docs/reference/pyproject-toml.md`, `CHANGELOG.md`

- [ ] **Step 1: Write the failing test** (append to `tests/test_migrate_config.py`)

```python
def test_a_lakebase_session_table_moves_to_session_kwarg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = (_PYPROJECT + '\n[tool.apx.agent.session]\ntype = "lakebase"\n'
            'host = "${LAKEBASE_HOST}"\ndatabase = "inspector"\n')
    root = _repo(tmp_path, _AGENT_PY_FULL)
    (root / "pyproject.toml").write_text(text)
    _commit_all(root)
    first = _run(root, monkeypatch)
    assert first.exit_code == 0, first.output
    assert "session={'type': 'lakebase', 'host': '${LAKEBASE_HOST}', 'database': 'inspector'}" in first.output
    (root / "agent.py").write_text(_AGENT_PY_FULL.replace(
        "examples=['Show tables', 'Count rows'])",
        "examples=['Show tables', 'Count rows'],\n"
        "              session={'type': 'lakebase', 'host': '${LAKEBASE_HOST}', 'database': 'inspector'})"))
    _commit_all(root)
    second = _run(root, monkeypatch)
    assert second.exit_code == 0, second.output
    assert "agent" not in tomllib.loads((root / "pyproject.toml").read_text())["tool"]["apx"]
```

Also make the knob's "lakebase" error point at the new home. In `test_memory_objects.py`, add:

```python
def test_lakebase_knob_error_points_at_session_and_memory_objects() -> None:
    with pytest.raises(ValueError, match="session=SessionBackendConfig"):
        Agent(name="o", tools=[], memory="lakebase")
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_migrate_config.py tests/test_memory_objects.py -q -p no:cacheprovider -k "lakebase"`
Expected:
- The migrate test may already pass after Task 1. The snippet path is unchanged code, and this test pins the end-to-end behavior. If it passes, note that in the report.
- The knob-error test FAILs, because the message still names `[tool.apx.agent.memory]`.

- [ ] **Step 3: Implement.** In `_models.normalize_memory_knob`, change the `"lakebase"` `ValueError` text to:

```python
            "memory='lakebase' needs connection details the one-word knob can't "
            "carry — pass memory=MemoryBackendConfig(type='lakebase', host=..., database=..., "
            "table_name=...) and session=SessionBackendConfig(type='lakebase', host=..., "
            "database=...) on the agent (a dict works too)."
```

Keep whatever follows in that `raise` (if the message continues past this sentence, replace only the sentence that names the TOML blocks).

- [ ] **Step 4: Docs.**
  - In `docs/reference/pyproject-toml.md`, under "Setting these in Python", add: "Explicit memory and session backends go on the agent: `memory=MemoryBackendConfig(...)` and `session=SessionBackendConfig(...)`, or the same as a dict."
  - `CHANGELOG.md` under `### Added`: `- **Memory and session backends on the agent.** memory= also accepts a MemoryBackendConfig (or a dict), and session= accepts a SessionBackendConfig (or a dict), so explicit Lakebase backends no longer need [tool.apx.agent.memory]/[tool.apx.agent.session].`

- [ ] **Step 5: Gate and commit**

Run: `make check`; `pre-commit run --files $(git diff --name-only origin/main...HEAD)`; the PR CI subset under xdist.
Expected: all green.

```bash
git add python/src/apx_agent/_models.py python/tests/test_migrate_config.py python/tests/test_memory_objects.py docs/reference/pyproject-toml.md CHANGELOG.md
git commit --quiet --message "docs: Lakebase backends move to memory=/session= on the agent

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

## Out of this plan

- The generators (3b-3b).
