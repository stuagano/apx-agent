# apx runtime settings on the Python agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every agent class declares apx runtime settings; one function merges them with `[tool.apx.agent]`, failing on conflicts, and leaves get their own model and identity.

**Architecture:** `BaseAgent` gains the settings as class-level `None` defaults plus a `_set_settings` validator; every constructor takes `**settings: Unpack[AgentSettings]`. A new `_settings.resolve_settings(agent, toml) -> AgentConfig` walks the tree once, raises on conflicts, and writes each node's per-agent values that differ from the app value into `node._overrides`. Compile and `finalize_agent` read `_overrides`; every `config_from_agent` caller switches to `resolve_settings`.

**Tech Stack:** Python ≥3.10, pydantic v2 (`AgentConfig`), `typing_extensions.Unpack`, pytest.

**Spec:** `docs/design/agent-toml-source-of-truth.md`, section "Sub-project 2: settings on the agent".

## Global Constraints

- Run tests from `python/`: `cd python && uv run pytest …`. Full gate: `make check` and `pre-commit run --all-files` from the repo root.
- Lint bans: `.get(k, "")`, `x or ""`, `getattr(x, "attr", "<string literal>")`, `object` annotations, `str = ""` defaults, invented env defaults, `tuple[...]` returns, unconditional `@pytest.mark.skip`.
- Error text, verbatim:
  - `{field}: agent.py sets {ours!r} on agent {label!r}; [tool.apx.agent] sets {theirs!r}. Set it in one place.`
  - `{field} is app-wide; set it on the root agent (found on {label!r}).`
  - launcher: `The native launcher requires target='durable_agent_server' on the root agent or in [tool.apx.agent]`
- Per-agent fields: `model`, `identity`. App-wide fields: `target`, `recovery`, `gateway`, `service_policies`, `guardrails`, `knowledge`, `vector_search_index`, `bindings`, `url`, `registry`, `api_prefix`. `memory`/`session` come from the root's `memory_config`/`session_config`.
- `.toml`-only projects must behave exactly as before; the existing suite stays green unchanged except tests that import the deleted `config_from_agent`.
- Commits end with `Co-authored-by: Isaac <no-reply@databricks.com>`. Never `git stash`. Before committing, `grep -c 'pypi-proxy\|databricks' python/uv.lock` must be 0 (or the file untouched).

## Review Focus

1. A setting name typo (`modle="x"`) on any agent class: expected `TypeError`, not a silent accept. Pinned in Task 1.
2. `resolve_settings` called twice on the same tree (deploy then compile, or `finalize_agent` then the launcher): expected identical result, no error. Pinned in Task 2.
3. `APX_MODEL` set while the root declares `model=`: expected the pin wins for the root and inheriting leaves. Pinned in Task 4.
4. An agent shared under two parents (the same leaf object reused): expected it is visited once, no false app-wide error. Pinned in Task 2.
5. A `.toml`-only project with an agent that declares nothing: expected `resolve_settings` returns a config equal to the `.toml` config. Pinned in Task 2.

---

### Task 1: Settings on every agent class

**Files:**
- Modify: `python/src/apx_agent/_agents.py` (BaseAgent ~line 87; LlmAgent `__init__` ~155–275; LoopAgent 714; SequentialAgent 804; ParallelAgent 899; RouterAgent 1006; HandoffAgent 1152; KeywordRouter 1269)
- Test: `python/tests/test_agent_settings.py`

**Interfaces:**
- Produces: `AgentSettings` (TypedDict, `total=False`) in `_agents.py`; class attributes on `BaseAgent` for every field (default `None`) and `_overrides: Mapping[str, Any] = MappingProxyType({})`; `BaseAgent._set_settings(self, settings: AgentSettings) -> None`. Every constructor accepts `**settings: Unpack[AgentSettings]`. `LlmAgent.recovery` default becomes `None` (was `False`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_agent_settings.py`)

```python
from apx_agent import HandoffAgent, LoopAgent, ParallelAgent, RouterAgent, SequentialAgent
from apx_agent._agents import KeywordRouter


def _leaf(name: str = "leaf", **settings: Any) -> Agent:
    return Agent(name=name, tools=[_noop], **settings)


@pytest.mark.parametrize("build", [
    lambda **s: SequentialAgent([_leaf()], **s),
    lambda **s: ParallelAgent([_leaf()], **s),
    lambda **s: RouterAgent([_leaf()], **s),
    lambda **s: LoopAgent(_leaf(), **s),
    lambda **s: HandoffAgent([_leaf()], **s),
    lambda **s: KeywordRouter([("a", _leaf(), ["a"])], default=_leaf("d"), **s),
])
def test_every_agent_class_takes_settings(build: Any) -> None:
    agent = build(model="m", target="durable_agent_server", knowledge="./okf")
    assert (agent.model, agent.target, agent.knowledge) == ("m", "durable_agent_server", "./okf")


def test_unset_settings_are_none() -> None:
    agent = SequentialAgent([_leaf()])
    assert agent.model is None and agent.recovery is None and agent.identity is None


def test_unknown_setting_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="modle"):
        SequentialAgent([_leaf()], modle="m")  # type: ignore[call-arg]


def test_composite_rejects_invalid_identity_and_recovery_on_responses() -> None:
    with pytest.raises(ValueError, match="identity"):
        SequentialAgent([_leaf()], identity="not-a-mode")
    with pytest.raises(ValueError, match="recovery requires"):
        SequentialAgent([_leaf()], recovery=True, target="responses_agent")
```

Check the exported names first: `grep -n "HandoffAgent\|LoopAgent\|RouterAgent\|ParallelAgent\|SequentialAgent" python/src/apx_agent/__init__.py`. Import any that are not exported from `apx_agent._agents` instead.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_agent_settings.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'model'` on composites.

- [ ] **Step 3: Implement**

In `_agents.py`, near the imports:

```python
from types import MappingProxyType
from typing_extensions import TypedDict, Unpack

from ._models import GuardrailsConfig  # add to the existing ._models import list


class AgentSettings(TypedDict, total=False):
    """apx runtime settings any agent may declare (agent-toml-source-of-truth.md, sub-project 2)."""

    model: str
    target: RuntimeTarget
    identity: str
    recovery: bool
    gateway: GatewayConfig
    service_policies: ServicePoliciesConfig
    guardrails: GuardrailsConfig
    knowledge: str
    vector_search_index: str
    bindings: dict[str, str]
    url: str
    registry: str
    api_prefix: str
```

On `BaseAgent` (class body, before `run`):

```python
    model: str | None = None
    target: RuntimeTarget | None = None
    identity: str | None = None
    recovery: bool | None = None
    gateway: GatewayConfig | None = None
    service_policies: ServicePoliciesConfig | None = None
    guardrails: GuardrailsConfig | None = None
    knowledge: str | None = None
    vector_search_index: str | None = None
    bindings: dict[str, str] | None = None
    url: str | None = None
    registry: str | None = None
    api_prefix: str | None = None
    _overrides: Mapping[str, Any] = MappingProxyType({})
    """Per-agent values that differ from the app's; written by resolve_settings."""

    def _set_settings(self, settings: AgentSettings) -> None:
        unknown = sorted(set(settings) - set(AgentSettings.__annotations__))
        if unknown:
            raise TypeError(f"unknown agent setting(s): {', '.join(unknown)}")
        identity = settings.get("identity")
        if identity is not None:
            from ._tool_scope import _validate_identity

            try:
                _validate_identity(identity)
            except ValueError as exc:
                raise ValueError(f"identity: {exc}") from exc
        if settings.get("recovery") and settings.get("target") == "responses_agent":
            raise ValueError("recovery requires target='durable_agent_server'")
        for key, value in settings.items():
            setattr(self, key, value)
```

Import `Mapping` from `collections.abc` if not already imported.

In `LlmAgent.__init__`: delete the six parameters `model`, `target`, `identity`, `recovery`, `gateway`, `service_policies` and the block that validates and assigns them (`self.model = model` … `self.service_policies = service_policies`). Add `**settings: Unpack[AgentSettings],` as the last parameter and call `self._set_settings(settings)` where that block was.

In each composite `__init__` (LoopAgent, SequentialAgent, ParallelAgent, RouterAgent, HandoffAgent, KeywordRouter): add `**settings: Unpack[AgentSettings],` as the last parameter and `self._set_settings(settings)` as the first statement of the body.

`DataAgent` and `CoworkerAgent` already forward `**kwargs` to `LlmAgent`; no change.

- [ ] **Step 4: Run to verify they pass, plus the agent suites**

Run: `cd python && uv run pytest tests/test_agent_settings.py tests/test_agents.py -q`
Expected: PASS. (If `tests/test_agents.py` doesn't exist, run `uv run pytest -q -k "agent" --co -q | head` to find the agent-construction suites, and run those.)

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_agents.py python/tests/test_agent_settings.py
git commit -m "feat: every agent class declares apx runtime settings

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 2: `resolve_settings`

**Files:**
- Create: `python/src/apx_agent/_settings.py`
- Test: `python/tests/test_resolve_settings.py`

**Interfaces:**
- Consumes: Task 1's `BaseAgent` attributes, `_overrides`.
- Produces: `resolve_settings(agent: BaseAgent, toml: AgentConfig | None) -> AgentConfig`; `PER_AGENT: tuple[str, ...]`; `APP_WIDE: tuple[str, ...]`. Side effect: sets `node._overrides` on every node in the tree.

- [ ] **Step 1: Write the failing tests**

```python
"""resolve_settings: the one merge of agent-declared settings with [tool.apx.agent]."""

from __future__ import annotations

import pytest

from apx_agent import Agent, SequentialAgent
from apx_agent._models import AgentConfig
from apx_agent._settings import resolve_settings


def _noop(q: str) -> str:
    """Echo."""
    return q


def _leaf(name: str = "leaf", **settings) -> Agent:
    return Agent(name=name, tools=[_noop], **settings)


def _toml(**fields) -> AgentConfig:
    return AgentConfig.model_validate({"name": "app", **fields})


def test_python_only_builds_config_from_root() -> None:
    root = _leaf("triage", model="m", identity="obo", recovery=True, memory="managed")
    config = resolve_settings(root, None)
    assert (config.name, config.model, config.identity, config.recovery) == ("triage", "m", "obo", True)
    assert config.target == "durable_agent_server"
    assert config.memory is not None and config.memory.store_name == root.memory_config.store_name


def test_toml_only_is_unchanged() -> None:
    toml = _toml(model="m", identity="obo")
    assert resolve_settings(_leaf(), toml) == toml


def test_equal_values_in_both_places_pass() -> None:
    assert resolve_settings(_leaf(model="m"), _toml(model="m")).model == "m"


def test_python_fills_fields_toml_leaves_unset() -> None:
    config = resolve_settings(_leaf(identity="obo"), _toml(model="m"))
    assert (config.model, config.identity, config.name) == ("m", "obo", "app")


def test_conflict_with_toml_fails() -> None:
    with pytest.raises(ValueError, match=r"model: agent.py sets 'a' on agent 'root'; \[tool.apx.agent\] sets 'b'. Set it in one place."):
        resolve_settings(_leaf("root", model="a"), _toml(model="b"))


def test_app_wide_field_below_root_must_match() -> None:
    same = SequentialAgent([_leaf("researcher", target="durable_agent_server")], target="durable_agent_server")
    resolve_settings(same, None)
    with pytest.raises(ValueError, match=r"target is app-wide; set it on the root agent \(found on 'researcher'\)."):
        resolve_settings(SequentialAgent([_leaf("researcher", target="responses_agent")]), None)


def test_per_agent_values_inherit_and_only_differences_are_overrides() -> None:
    inner = _leaf("inner")
    middle = SequentialAgent([inner], model="special", identity="obo")
    plain = _leaf("plain")
    root = SequentialAgent([middle, plain], model="app")
    config = resolve_settings(root, None)
    assert config.model == "app"
    assert dict(inner._overrides) == {"model": "special", "identity": "obo"}
    assert dict(plain._overrides) == {}
    assert dict(root._overrides) == {}


def test_resolve_twice_is_identical() -> None:
    inner = _leaf("inner", model="special")
    root = SequentialAgent([inner], model="app", target="durable_agent_server")
    first = resolve_settings(root, _toml(model="app"))
    assert resolve_settings(root, _toml(model="app")) == first
    assert dict(inner._overrides) == {"model": "special"}


def test_shared_leaf_is_visited_once() -> None:
    shared = _leaf("shared", target="durable_agent_server")
    root = SequentialAgent([shared, shared], target="durable_agent_server")
    assert resolve_settings(root, None).target == "durable_agent_server"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_resolve_settings.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'apx_agent._settings'`.

- [ ] **Step 3: Implement** `python/src/apx_agent/_settings.py`

```python
"""apx runtime settings declared on the agent tree, merged with [tool.apx.agent].

See docs/design/agent-toml-source-of-truth.md, "Sub-project 2"."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from ._models import AgentConfig

if TYPE_CHECKING:
    from ._agents import BaseAgent

PER_AGENT = ("model", "identity")
APP_WIDE = ("target", "recovery", "gateway", "service_policies", "guardrails", "knowledge",
            "vector_search_index", "bindings", "url", "registry", "api_prefix")
_ROOT_ONLY = {"memory": "memory_config", "session": "session_config"}


def _label(agent: BaseAgent) -> str:
    name = getattr(agent, "_name", None)
    return name if name else type(agent).__name__


def _tree(root: BaseAgent) -> list[tuple[BaseAgent, BaseAgent | None]]:
    """(agent, parent) pairs breadth-first, so a parent precedes its children; each agent once."""
    from ._topology import _iter_child_agents
    from ._wiring import _agent_tool_nested_agents

    pairs: list[tuple[BaseAgent, BaseAgent | None]] = []
    seen: set[int] = set()
    queue: list[tuple[BaseAgent, BaseAgent | None]] = [(root, None)]
    while queue:
        node, parent = queue.pop(0)  # ponytail: list as queue; agent trees are small
        if id(node) in seen:
            continue
        seen.add(id(node))
        pairs.append((node, parent))
        queue += [(child, node) for _, child in _iter_child_agents(node)]
        queue += [(child, node) for child in _agent_tool_nested_agents(node)]
    return pairs


def resolve_settings(agent: BaseAgent, toml: AgentConfig | None) -> AgentConfig:
    """The app's effective AgentConfig. Raises ValueError on a conflict.

    Writes each node's per-agent values that differ from the app's into
    ``node._overrides``; recomputed on every call, so calling twice is safe."""
    declared: dict[str, Any] = {
        f: getattr(agent, f) for f in (*PER_AGENT, *APP_WIDE) if getattr(agent, f) is not None
    }
    declared.update({
        f: getattr(agent, attr) for f, attr in _ROOT_ONLY.items() if getattr(agent, attr, None) is not None
    })
    name = getattr(agent, "_name", None)
    ours = AgentConfig.model_validate({"name": name if name else "agent", **declared})
    if toml is None:
        config = ours
    else:
        for field in declared:
            theirs, mine = getattr(toml, field), getattr(ours, field)
            if field in toml.model_fields_set and theirs != mine:
                raise ValueError(f"{field}: agent.py sets {mine!r} on agent {_label(agent)!r}; "
                                 f"[tool.apx.agent] sets {theirs!r}. Set it in one place.")
        config = toml.model_copy(update={f: getattr(ours, f) for f in declared})

    effective: dict[int, dict[str, Any]] = {}
    for node, parent in _tree(agent):
        if parent is None:
            effective[id(node)] = {f: getattr(config, f) for f in PER_AGENT}
        else:
            for field in APP_WIDE:
                value = getattr(node, field)
                if value is not None and value != getattr(config, field):
                    raise ValueError(f"{field} is app-wide; set it on the root agent (found on {_label(node)!r}).")
            inherited = effective[id(parent)]
            effective[id(node)] = {
                f: getattr(node, f) if getattr(node, f) is not None else inherited[f] for f in PER_AGENT
            }
        node._overrides = MappingProxyType(
            {f: v for f, v in effective[id(node)].items() if v is not None and v != getattr(config, f)}
        )
    return config
```

Note: `toml.model_copy(update=…)` skips cross-field validation; the values in `ours` were already validated field by field. If a test shows a cross-field rule being bypassed, switch to `AgentConfig.model_validate({**toml.model_dump(exclude_unset=True), **declared})` and confirm the round-trip in the test.

- [ ] **Step 4: Run to verify they pass**

Run: `cd python && uv run pytest tests/test_resolve_settings.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_settings.py python/tests/test_resolve_settings.py
git commit -m "feat: resolve_settings merges agent settings with [tool.apx.agent]

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 3: Every entry point uses `resolve_settings`

**Files:**
- Modify: `python/src/apx_agent/_serve.py:10-35`
- Modify: `python/src/apx_agent/_project_gen.py:260-270` (generated `app.py` template)
- Modify: `python/src/apx_agent/cli.py:311-323` (`_config_from_project_or_agent`)
- Modify: `python/src/apx_agent/_doctor_durable.py:274-290`
- Modify: `python/src/apx_agent/_inspection.py:316-345` (delete `config_from_agent`)
- Test: `python/tests/test_agent_settings.py` (update the tests that import `config_from_agent`; add new ones)

**Interfaces:**
- Consumes: `resolve_settings(agent, toml) -> AgentConfig` from Task 2.
- Produces: `config_from_agent` no longer exists.

- [ ] **Step 1: Write the failing tests** (in `tests/test_agent_settings.py`)

Replace `from apx_agent._inspection import config_from_agent` with `from apx_agent._settings import resolve_settings`. In `test_config_from_agent_copies_constructor_settings`, rename it to `test_resolve_settings_copies_constructor_settings` and replace `config_from_agent(agent)` with `resolve_settings(agent, None)`. Then add:

```python
def test_launcher_fails_on_conflict_with_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys
    from apx_agent._serve import create_app

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\n\n[tool.apx.agent]\nname = "p"\n'
        'model = "toml-model"\ntarget = "durable_agent_server"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(agent=Agent(name="p", tools=[_noop], model="py-model")))
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **k: MagicMock())
    monkeypatch.setattr("apx_agent._runtime_targets.compile_agent", MagicMock())
    with pytest.raises(ValueError, match="Set it in one place"):
        create_app()


def test_launcher_accepts_a_composite_python_only_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys
    from apx_agent import SequentialAgent
    from apx_agent._serve import create_app

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    root = SequentialAgent([Agent(name="a", tools=[_noop])], model="m", target="durable_agent_server")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(agent=root))
    monkeypatch.delenv("APX_MODEL", raising=False)
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **k: MagicMock())
    compiler = MagicMock(return_value=SimpleNamespace(state=SimpleNamespace()))
    monkeypatch.setattr("apx_agent._runtime_targets.compile_agent", compiler)
    create_app()
    assert compiler.call_args.kwargs["model"] == "m"
    assert compiler.call_args.kwargs["config"].target == "durable_agent_server"


def test_launcher_rejects_python_only_responses_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys
    from apx_agent._serve import create_app

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(
        agent=Agent(name="p", tools=[_noop], model="m", target="responses_agent")))
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **k: MagicMock())
    with pytest.raises(ValueError, match="on the root agent or in \\[tool.apx.agent\\]"):
        create_app()


def test_durable_doctor_reports_a_conflict_as_config_fail(tmp_path: Path) -> None:
    from apx_agent._doctor import Status
    from apx_agent._doctor_durable import check_durable_readiness

    root = tmp_path / "p"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0.1.0"\n\n[tool.apx.agent]\nname = "p"\nmodel = "toml-model"\n')
    (root / "agent.py").write_text(
        "from apx_agent import LlmAgent\nagent = LlmAgent(name='p', tools=[], model='py-model')\n")
    checks = {c.name: c for c in check_durable_readiness(root)}
    assert checks["Config"].status is Status.FAIL
    assert "Set it in one place" in checks["Config"].detail


def test_durable_doctor_loads_a_composite_python_only_root(tmp_path: Path) -> None:
    from apx_agent._doctor import Status
    from apx_agent._doctor_durable import check_durable_readiness

    root = tmp_path / "p"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0.1.0"\n')
    (root / "agent.py").write_text(
        "from apx_agent import LlmAgent, SequentialAgent\n"
        "agent = SequentialAgent([LlmAgent(name='a', tools=[])], model='m', target='durable_agent_server')\n")
    checks = {c.name: c for c in check_durable_readiness(root)}
    assert checks["Config"].status is Status.OK
    assert "SequentialAgent" in checks["Config"].detail


def test_deploy_config_merges_agent_and_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import cli

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\n\n[tool.apx.agent]\nname = "p"\nmodel = "toml-model"\n')
    (tmp_path / "agent.py").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_load_finalized_agent", lambda module: Agent(name="p", tools=[_noop], model="py-model"))
    with pytest.raises(ValueError, match="Set it in one place"):
        cli._config_from_project_or_agent(tmp_path, "agent:agent")
    monkeypatch.setattr(cli, "_load_finalized_agent", lambda module: Agent(name="p", tools=[_noop], identity="obo"))
    config, agent = cli._config_from_project_or_agent(tmp_path, "agent:agent")
    assert (config.model, config.identity, agent.identity) == ("toml-model", "obo", "obo")
```

Read `check_durable_readiness`'s Config stage first (`_doctor_durable.py:265-300`) to confirm an exception inside its `try` becomes `Status.FAIL` with the message in `detail`; adjust the conflict test's assertion to that stage's actual detail format if it wraps the message.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_agent_settings.py -q`
Expected: FAIL — `ModuleNotFoundError`/`ImportError` is gone after the import swap; the new tests fail because conflicts aren't detected and composites don't carry `model`.

- [ ] **Step 3: Implement**

`_serve.py` body of `create_app` becomes:

```python
    from ._defaults import _make_workspace_client
    from ._inspection import _load_agent_config
    from ._runtime_targets import compile_agent
    from ._settings import resolve_settings
    from ._wiring import resolve_agent

    pyproject = Path.cwd() / "pyproject.toml"
    toml = _load_agent_config(pyproject_path=pyproject)
    # Pin tool/config loading too, including reload workers and inherited shells.
    os.environ["APX_PYPROJECT"] = str(pyproject)
    ws = _make_workspace_client()
    agent = resolve_agent("agent:agent", toml, ws=ws)
    config = resolve_settings(agent, toml)
    if config.target != "durable_agent_server":
        raise ValueError(
            "The native launcher requires target='durable_agent_server' on the root agent or in [tool.apx.agent]")
    model = os.environ.get("APX_MODEL", config.model)
```

(keep the rest of the function — the `compile_agent(...)` call and `app.state` lines — unchanged).

`_project_gen.py` template lines become:

```python
from apx_agent._inspection import _load_agent_config
from apx_agent._settings import resolve_settings

config = resolve_settings(agent, _load_agent_config())
```

(delete the `if config is None:` / `config = config_from_agent(agent)` lines).

`cli.py` `_config_from_project_or_agent` becomes:

```python
def _config_from_project_or_agent(cwd: Path, module: str):
    """The effective config: ``[tool.apx.agent]`` merged with the Python agent's settings."""
    from ._inspection import _load_agent_config
    from ._settings import resolve_settings

    config = _load_agent_config(pyproject_path=cwd / "pyproject.toml")
    if (cwd / "databricks.yml").exists() or (cwd / "databricks.yml").is_symlink():
        return config, None
    if not (cwd / "agent.py").is_file() and module == "agent:agent":
        return config, None
    agent = _load_finalized_agent(module)
    return resolve_settings(agent, config), agent
```

Its caller at `cli.py:~10599` already catches `ValueError` and raises `click.ClickException`, so the conflict message reaches the user before upload.

`_doctor_durable.py`: in the Config stage, import `resolve_settings` from `._settings` instead of `config_from_agent`; in the `python_only` branch use `config = resolve_settings(agent, None)`; in the `.toml` branch add `config = resolve_settings(agent, config)` directly after `agent = resolve_agent(...)`.

`_inspection.py`: delete `config_from_agent`. Then `grep -rn config_from_agent python/` must return nothing.

- [ ] **Step 4: Run to verify**

Run: `cd python && uv run pytest tests/test_agent_settings.py tests/test_doctor_durable.py tests/test_runtime_targets.py tests/test_project_gen.py -q`
Expected: PASS. (If `tests/test_project_gen.py` doesn't exist, find the generated-`app.py` test with `grep -rln "_load_agent_config()" python/tests`.)

- [ ] **Step 5: Ctk read-back of the generated app.py**

Find the test that renders the durable `app.py` (`grep -rn "compile_agent" python/tests/*project_gen* python/tests/*reality_ctk*`) and add to it:

```python
from ctk import Artifact, verify

verify(Artifact(app_py_path, min_bytes=200, must_contain=["resolve_settings(agent, _load_agent_config())"]))
```

Run that test; expected PASS.

- [ ] **Step 6: Commit**

```bash
git add python/src/apx_agent/_serve.py python/src/apx_agent/_project_gen.py python/src/apx_agent/cli.py \
  python/src/apx_agent/_doctor_durable.py python/src/apx_agent/_inspection.py python/tests/
git commit -m "feat: launcher, app.py, deploy and doctor merge settings through resolve_settings

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 4: Per-leaf model and identity

**Files:**
- Modify: `python/src/apx_agent/_compile.py:453-457` and `:939`
- Modify: `python/src/apx_agent/_wiring.py:575-610` (`finalize_agent` identity blocks)
- Test: `python/tests/test_resolve_settings.py`

**Interfaces:**
- Consumes: `node._overrides` (Task 2), `resolve_settings` (Task 2), `apply_agent_identity(agent, identity)` (`_agent_identity.py:53`, stamps `agent._tool_fns`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_resolve_settings.py`)

```python
from apx_agent._compile import CompileContext, compile_to_langgraph
from apx_agent._tool_scope import get_scope
from apx_agent._wiring import finalize_agent


def test_leaf_compiles_with_its_own_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = __import__("apx_agent._compile", fromlist=["_build_chat_databricks"])._build_chat_databricks
    monkeypatch.setattr("apx_agent._compile._build_chat_databricks",
                        lambda model, **k: seen.append(model) or real(model, **k))
    root = SequentialAgent([_leaf("a", model="special"), _leaf("b")], model="app")
    resolve_settings(root, None)
    compile_to_langgraph(root, CompileContext(service_ws=None, user_ws=None, model="app"))
    assert sorted(seen) == ["app", "special"]


def test_apx_model_pin_wins_for_inheriting_leaves(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = __import__("apx_agent._compile", fromlist=["_build_chat_databricks"])._build_chat_databricks
    monkeypatch.setattr("apx_agent._compile._build_chat_databricks",
                        lambda model, **k: seen.append(model) or real(model, **k))
    root = SequentialAgent([_leaf("a"), _leaf("b", model="special")], model="app")
    resolve_settings(root, None)
    compile_to_langgraph(root, CompileContext(service_ws=None, user_ws=None, model="pinned"))
    assert sorted(seen) == ["pinned", "special"]


def test_each_leafs_tools_get_its_own_identity() -> None:
    def tool_a(q: str) -> str:
        """A."""
        return q

    def tool_b(q: str) -> str:
        """B."""
        return q

    a = Agent(name="a", tools=[tool_a], identity="obo")
    b = Agent(name="b", tools=[tool_b])
    root = SequentialAgent([a, b], identity="sp", target="durable_agent_server", model="m")
    finalize_agent(root, resolve_settings(root, None))
    assert get_scope(a._tool_fns[0]).identity == "obo"
    assert get_scope(b._tool_fns[0]).identity == "sp"
```

Before running, check: (1) `compile_to_langgraph`'s signature at `_compile.py:1550` — adjust the call if it takes the model/context differently; (2) `CompileContext` required fields at `_compile.py:101`; (3) whether `finalize_agent` needs `ws=` for this agent (pass `ws=MagicMock()` if it requires one); (4) valid identity strings in `_tool_scope._validate_identity` — replace `"obo"`/`"sp"` with two valid, distinct values if those aren't accepted.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_resolve_settings.py -q -k "model or identity"`
Expected: FAIL — every leaf builds with `ctx.model`; `b`'s tool has no identity.

- [ ] **Step 3: Implement**

`_compile.py` line ~454: replace `ctx.model,` with `agent._overrides.get("model", ctx.model),`.
`_compile.py` line ~939: replace `_build_chat_databricks(ctx.model)` with `_build_chat_databricks(agent._overrides.get("model", ctx.model))`.

`_wiring.py` `finalize_agent`: replace the identity block inside the config branch:

```python
        identity = getattr(agent, "identity", None)
        if identity is None:
            identity = config.identity
        if identity is not None:
            from ._agent_identity import apply_agent_identity  # noqa: PLC0415

            apply_agent_identity(agent, identity)
```

with a call to a new helper, and replace the `constructor_identity` block after the config branch with the same helper:

```python
def _apply_leaf_identities(agent: BaseAgent, config: AgentConfig | None) -> None:
    """Stamp each leaf's effective identity onto that leaf's tools that declare none."""
    from ._agent_identity import apply_agent_identity  # noqa: PLC0415
    from ._settings import resolve_settings  # noqa: PLC0415

    app = resolve_settings(agent, config)
    for leaf in _collect_guardrail_targets(agent):
        identity = leaf._overrides.get("identity", app.identity)
        if identity is not None:
            apply_agent_identity(leaf, identity)
```

Call `_apply_leaf_identities(agent, config)` where the first block was (inside the config branch) and delete the trailing `constructor_identity` block, adding `if config is None: _apply_leaf_identities(agent, None)` in its place. `resolve_settings` raising here is intended: conflicts fail at finalize too.

- [ ] **Step 4: Run to verify, then the full gate**

Run: `cd python && uv run pytest tests/test_resolve_settings.py tests/test_agent_identity.py tests/test_tool_scoped_auth.py -q`
Expected: PASS.
Run from the repo root: `make check` then `pre-commit run --all-files`.
Expected: both green. Any failure in an existing test is a regression to fix here, not a test to edit (Global Constraints).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_compile.py python/src/apx_agent/_wiring.py python/tests/test_resolve_settings.py
git commit -m "feat: leaves compile with their own model and stamp their own identity

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

### Task 5: Docs and changelog

**Files:**
- Modify: the configuration reference (`grep -rln "tool.apx.agent\]" docs/ | grep -i config` — the page listing `[tool.apx.agent]` fields)
- Modify: `CHANGELOG.md` (Unreleased section)

- [ ] **Step 1: Add "set it in Python" to the configuration reference**

Add this subsection under the `[tool.apx.agent]` field table:

````markdown
### Setting these in Python

Any agent accepts the runtime fields as keyword arguments. A field set both in
Python and in `[tool.apx.agent]` must have the same value.

```python
# before: pyproject.toml
# [tool.apx.agent]
# model = "system.ai.claude-sonnet-4-6"
# target = "durable_agent_server"

# after: agent.py
agent = SequentialAgent(
    [researcher, Agent(name="writer", tools=[draft], model="system.ai.claude-opus-4-7")],
    model="system.ai.claude-sonnet-4-6",
    target="durable_agent_server",
)
```

`model` and `identity` are per-agent: an agent uses its own value, else its
nearest ancestor's, else the app's. Every other field is app-wide: set it on
the root; a different value on a child fails.
````

- [ ] **Step 2: CHANGELOG entry** under Unreleased:

```markdown
- Every agent class takes apx runtime settings; `resolve_settings` merges them with `[tool.apx.agent]` and fails on conflicting values. Leaves compile with their own `model` and stamp their own `identity`.
```

- [ ] **Step 3: Verify and commit**

Run: `pre-commit run --all-files` (doc-claim tests run in `make check`; run `make check` too if the reference page is covered by `tests/test_docs_*`).

```bash
git add docs/ CHANGELOG.md
git commit -m "docs: setting apx runtime fields in Python

Co-authored-by: Isaac <no-reply@databricks.com>"
```

---

## Out of this plan

- Scaffold writing settings in `agent.py`: sub-project 3, with the removal of `[tool.apx.agent]`.
- #954: already fixed by #961 and #974 (`test_python_only_native_deploy_does_not_need_pyproject_table` shows deploy runs the `agent.toml` checks). The PR body closes it.
- Live proof on a workspace: optional; asks first.
