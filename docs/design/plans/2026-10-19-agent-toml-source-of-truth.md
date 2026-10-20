# agent.toml as the Platform's Source of Truth — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** apx reads and checks a project's authored `agent.toml` instead of generating one, and apx agents can call `agent.toml` tools through `platform_tools()` under apx's per-call guards.

**Architecture:** One new module reads `agent.toml` with the SDK's `AgentProject.load` and returns doctor `Check` findings. A second new module holds `platform_tools()` placeholders and turns them into LangChain tools through AgentKit's `genie_tools()` / MCP client. The compiler appends those tools to the leaf's tool list, where the existing callback handler already runs `before_tool` guards by tool name. Deploy stages the authored file unchanged and falls back to today's generated manifest when none exists.

**Tech Stack:** Python 3.11+, `databricks-agentbricks` (`databricks_agentbricks.projects.agent_project.AgentProject`, `databricks_agentkit.langgraph`), LangGraph / `langchain_core.tools.BaseTool`, click, pytest, `ctk`.

**Spec:** `docs/design/agent-toml-source-of-truth.md`

## Global Constraints

- apx never writes `agent.toml`, except `apx-agent agents migrate-agent-toml`, which writes through `AgentProject.create(...).write()` and then adds `[auth.user]`.
- apx reads `agent.toml` only through `AgentProject.load`; no apx TOML parser for it.
- Every mismatch is `Status.FAIL`. The only `Status.WARN` is "no `agent.toml`" during the transition.
- A missing `agent.toml` keeps today's behavior byte for byte: `build_native_manifest` output in `.build/agent.toml`.
- A `ToolScope` on a platform tool is a compile error; the data ceiling is the platform's `downscope`.
- An `auth = "user"` platform tool never falls back to the service client.
- `platform_tools()` is only supported on `target="durable_agent_server"`; `responses_agent` with platform tools is a compile error.
- The only private AgentKit symbol apx may import is `databricks_agentkit.langgraph.mcp._server_from_tool`, and only inside `_platform_tools._mcp_server_for`, pinned by a test.
- Lint rules enforced by pre-commit apply: no `.get(k, "")`, no `x or ""`, no invented env defaults, no `object` annotations, no `tuple[...]` returns, no unconditional skips.
- Run Python via `cd python && uv run ...` (the root `.venv` is stale).

## Review Focus

1. **`agent.toml` in a parent directory.** AgentKit's `project_root()` walks up from cwd; apx must resolve the file at the project root it was given and pin `AGENTBRICKS_PROJECT_ROOT` to it, so a stray `~/agent.toml` is never read. Test in Task 2.
2. **Same `platform_tools()` id on two leaves.** Both leaves get the tool; that is not a collision. Collision is only same-name-within-one-leaf. Test in Task 1.
3. **An `auth = "user"` MCP tool called outside a request** (local `apx dev`, doctor). Locally AgentKit falls back to the env client; deployed it raises `AuthError`. apx must not add its own fallback. Test in Task 4.
4. **Authored `agent.toml` plus the old generated marker file already in `.build/`.** Staging replaces the stale generated file with the authored one; it never keeps the generated one. Test in Task 5.
5. **`migrate-agent-toml` run twice.** The second run refuses and leaves the file untouched. Test in Task 7.

---

## File Structure

| File | Responsibility |
|---|---|
| Create `python/src/apx_agent/_platform_tools.py` | `PlatformToolRef`, `platform_tools()`, leaf discovery, materialization through AgentKit |
| Create `python/src/apx_agent/_agent_toml.py` | locate + load `agent.toml`; `check_agent_toml()` findings; `stage_agent_toml()` |
| Modify `python/src/apx_agent/_agents.py` | `LlmAgent.__init__` separates `PlatformToolRef` from callables |
| Modify `python/src/apx_agent/_compile.py` | `CompileContext.platform_tools`; `_compile_llm_agent` appends them |
| Modify `python/src/apx_agent/_durable_agent.py` | startup + per-invocation materialization; `/readyz` reports failures |
| Modify `python/src/apx_agent/_runtime_targets.py` | compile-time checks; `responses_agent` rejection |
| Modify `python/src/apx_agent/cli.py` | deploy preflight + staging; `agents migrate-agent-toml` |
| Modify `python/src/apx_agent/_apps_api_deploy.py` | `_stage` uses `stage_agent_toml` |
| Modify `python/src/apx_agent/_doctor.py`, `_doctor_durable.py` | doctor rows |
| Modify `python/src/apx_agent/__init__.py` | export `platform_tools` |
| Tests | `python/tests/test_platform_tools.py`, `test_agent_toml.py`, `test_agent_toml_reality_ctk.py` |
| Docs | `docs/running/runtime-targets.md` section; `python/capabilities.yaml` (local, git-excluded) |

---

### Task 1: `platform_tools()` placeholders on `LlmAgent`

**Files:**
- Create: `python/src/apx_agent/_platform_tools.py`
- Modify: `python/src/apx_agent/_agents.py:186-187` (`LlmAgent.__init__`)
- Modify: `python/src/apx_agent/__init__.py` (export)
- Test: `python/tests/test_platform_tools.py`

**Interfaces:**
- Produces:
  - `class PlatformToolRef` — frozen dataclass, field `ids: frozenset[str]` (empty = all).
  - `def platform_tools(*ids: str) -> list[PlatformToolRef]` — returns a one-element list so `*platform_tools()` splats.
  - `LlmAgent._platform_tool_refs: list[PlatformToolRef]`.
  - `def platform_tool_leaves(agent: BaseAgent) -> list[tuple[LlmAgent, PlatformToolRef]]`.

- [ ] **Step 1: Write the failing tests**

```python
# python/tests/test_platform_tools.py
from __future__ import annotations

from apx_agent import Agent, SequentialAgent, platform_tools
from apx_agent._platform_tools import PlatformToolRef, platform_tool_leaves


def _noop(q: str) -> str:
    """Echo."""
    return q


def test_platform_tools_splats_into_tools_and_stays_out_of_callables() -> None:
    agent = Agent(name="a", tools=[_noop, *platform_tools("genie-pricing")])
    assert agent._tool_fns == [_noop]
    assert agent._platform_tool_refs == [PlatformToolRef(frozenset({"genie-pricing"}))]


def test_no_ids_means_all() -> None:
    [ref] = platform_tools()
    assert ref.ids == frozenset()


def test_leaves_found_in_composites_including_shared_ids() -> None:
    a = Agent(name="a", tools=[*platform_tools("g")])
    b = Agent(name="b", tools=[*platform_tools("g")])
    leaves = platform_tool_leaves(SequentialAgent([a, b], name="s"))
    assert [(leaf._name, ref.ids) for leaf, ref in leaves] == [
        ("a", frozenset({"g"})), ("b", frozenset({"g"})),
    ]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_platform_tools.py -q`
Expected: FAIL — `ImportError: cannot import name 'platform_tools'`.

- [ ] **Step 3: Implement**

```python
# python/src/apx_agent/_platform_tools.py
"""Tools declared in the project's agent.toml, referenced from Python (see
docs/design/agent-toml-source-of-truth.md). agent.toml says what platform tools
exist; platform_tools() says which agent uses them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._agents import BaseAgent, LlmAgent


@dataclass(frozen=True)
class PlatformToolRef:
    """Placeholder for agent.toml tools; resolved at startup or per request."""

    ids: frozenset[str]


def platform_tools(*ids: str) -> list[PlatformToolRef]:
    """Reference agent.toml tools by id (no ids = all). Splat into ``tools=[...]``."""
    return [PlatformToolRef(frozenset(ids))]


def platform_tool_leaves(agent: BaseAgent) -> list[tuple[LlmAgent, PlatformToolRef]]:
    """Every (LlmAgent leaf, ref) pair in the agent graph, in graph order."""
    from ._agents import LlmAgent
    from ._wiring import _collect_guardrail_targets

    return [
        (leaf, ref)
        for leaf in _collect_guardrail_targets(agent)
        if isinstance(leaf, LlmAgent)
        for ref in leaf._platform_tool_refs
    ]
```

In `_agents.py` `LlmAgent.__init__`, replace:

```python
        tools = [] if tools is None else tools
        self._tool_fns = tools
```

with:

```python
        from ._platform_tools import PlatformToolRef  # noqa: PLC0415

        tools = [] if tools is None else tools
        self._platform_tool_refs = [t for t in tools if isinstance(t, PlatformToolRef)]
        self._tool_fns = [t for t in tools if not isinstance(t, PlatformToolRef)]
```

In `__init__.py`, add `from ._platform_tools import platform_tools` next to the other public imports and `"platform_tools"` to `__all__`.

- [ ] **Step 4: Run to verify they pass**

Run: `cd python && uv run pytest tests/test_platform_tools.py -q`
Expected: 3 passed. If `_collect_guardrail_targets` does not include `SequentialAgent` children, use the leaf walk `ensure_scope_guard` relies on and keep the test.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_platform_tools.py python/src/apx_agent/_agents.py python/src/apx_agent/__init__.py python/tests/test_platform_tools.py
git commit -m "feat: platform_tools() placeholders for agent.toml tools"
```

---

### Task 2: Load and check `agent.toml`

**Files:**
- Create: `python/src/apx_agent/_agent_toml.py`
- Test: `python/tests/test_agent_toml.py`

**Interfaces:**
- Consumes: `platform_tool_leaves` (Task 1); `AuthorizationPlan` (`_apps_authorization.py:94`); `Check`, `Status` (`_doctor.py:61-75`).
- Produces:
  - `def load_agent_toml(project_root: Path) -> AgentProject | None` — `None` when `project_root / "agent.toml"` is absent; never walks up.
  - `def platform_tool_names(project: AgentProject) -> dict[str, list[str]]` — id → LLM-visible names (Genie: `<id>_ask`, `<id>_poll`, `<id>_query_result`; MCP kinds: names are discovered later, so the id itself).
  - `def check_agent_toml(project_root: Path, agent: BaseAgent, plan: AuthorizationPlan, config: AgentConfig | None, *, runtime_only: bool = False) -> list[Check]`.

- [ ] **Step 1: Write the failing tests**

```python
# python/tests/test_agent_toml.py
from __future__ import annotations

from pathlib import Path

import pytest

from apx_agent import Agent, platform_tools
from apx_agent._agent_toml import check_agent_toml, load_agent_toml
from apx_agent._apps_authorization import compile_authorization_plan
from apx_agent._doctor import Status

BASE = 'schema_version = 1\n\n[agent]\nframework = "langgraph"\nserver = "agentbricks"\n'
GENIE = '\n[[tools]]\nid = "pricing"\nauth = "user"\n[tools.source]\nkind = "genie_agent"\nspace_id = "01ef"\n'


def _project(tmp_path: Path, body: str) -> Path:
    (tmp_path / "agent.toml").write_text(body)
    return tmp_path


def _noop(q: str) -> str:
    """Echo."""
    return q


def _findings(root: Path, agent: Agent) -> list:
    plan = compile_authorization_plan(agent, model="m")
    return [c for c in check_agent_toml(root, agent, plan, None) if c.status is not Status.OK]


def test_absent_file_is_none_and_never_walks_up(tmp_path: Path) -> None:
    (tmp_path / "agent.toml").write_text(BASE)
    child = tmp_path / "project"
    child.mkdir()
    assert load_agent_toml(child) is None


def test_clean_project_has_no_findings(tmp_path: Path) -> None:
    root = _project(tmp_path, BASE + GENIE)
    assert _findings(root, Agent(name="a", tools=[_noop, *platform_tools("pricing")])) == []


def test_unknown_platform_id_fails_with_fix(tmp_path: Path) -> None:
    root = _project(tmp_path, BASE + GENIE)
    [finding] = _findings(root, Agent(name="a", tools=[*platform_tools("pricing", "nope")]))
    assert finding.status is Status.FAIL
    assert "nope" in finding.detail and "agent.toml" in finding.fix


def test_unreferenced_declared_tool_fails(tmp_path: Path) -> None:
    root = _project(tmp_path, BASE + GENIE)
    [finding] = _findings(root, Agent(name="a", tools=[_noop]))
    assert "pricing" in finding.detail and "platform_tools" in finding.fix


def test_name_collision_with_python_tool_fails(tmp_path: Path) -> None:
    def pricing_ask(q: str) -> str:
        """Clashes with the Genie tool name."""
        return q

    root = _project(tmp_path, BASE + GENIE)
    [finding] = _findings(root, Agent(name="a", tools=[pricing_ask, *platform_tools("pricing")]))
    assert "pricing_ask" in finding.detail


def test_missing_user_scope_fails(tmp_path: Path) -> None:
    from apx_agent import Dependencies

    def whoami(ws: Dependencies.UserClient) -> str:
        """Needs a user scope."""
        return ws.current_user.me().user_name

    root = _project(tmp_path, BASE)
    findings = _findings(root, Agent(name="a", tools=[whoami]))
    assert any("additional_api_scopes" in f.fix for f in findings)


def test_wrong_framework_fails(tmp_path: Path) -> None:
    root = _project(tmp_path, BASE.replace("langgraph", "openai"))
    [finding] = _findings(root, Agent(name="a", tools=[_noop]))
    assert "langgraph" in finding.fix


def test_unparseable_file_is_one_fail_not_a_traceback(tmp_path: Path) -> None:
    root = _project(tmp_path, "schema_version = 99\n")
    [finding] = _findings(root, Agent(name="a", tools=[_noop]))
    assert finding.status is Status.FAIL and finding.name == "agent.toml"


@pytest.mark.parametrize("runtime_only, expected", [(True, 0), (False, 1)])
def test_runtime_only_skips_deploy_level_checks(tmp_path: Path, runtime_only: bool, expected: int) -> None:
    root = _project(tmp_path, BASE + GENIE)
    agent = Agent(name="a", tools=[_noop])  # unreferenced tool: deploy-level, not runtime-breaking
    plan = compile_authorization_plan(agent, model="m")
    fails = [c for c in check_agent_toml(root, agent, plan, None, runtime_only=runtime_only)
             if c.status is Status.FAIL]
    assert len(fails) == expected
```

Add store tests in the same style, using `AgentConfig.model_validate({...})` with `memory` / `session` = `{"type": "managed", "store_name": ...}` (fields from `_models.py` `MemoryBackendConfig` / `SessionBackendConfig`): managed memory with no `[memory_store]` fails with a fix naming `agentbricks memory bind`; `store_name = "a"` against `[memory_store] name = "b"` fails; the same two for sessions; matching names pass.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_agent_toml.py -q`
Expected: FAIL — `ModuleNotFoundError: apx_agent._agent_toml`.

- [ ] **Step 3: Implement**

```python
# python/src/apx_agent/_agent_toml.py
"""agent.toml is the platform's source of truth (docs/design/agent-toml-source-of-truth.md).

apx reads it with the SDK's own AgentProject.load and checks it against the
Python agent. It never writes it (see `agents migrate-agent-toml` for the one
exception)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._doctor import Check, Status

if TYPE_CHECKING:
    from ._agents import BaseAgent
    from ._apps_authorization import AuthorizationPlan
    from ._models import AgentConfig

_NAME = "agent.toml"


def load_agent_toml(project_root: Path) -> Any | None:
    """The AgentProject at exactly ``project_root``, or None. Never walks up."""
    if not (project_root / _NAME).is_file():
        return None
    from databricks_agentbricks.projects.agent_project import AgentProject

    return AgentProject.load(project_root)


def platform_tool_names(project: Any) -> dict[str, list[str]]:
    """agent.toml id → the tool names the LLM sees for it."""
    names: dict[str, list[str]] = {}
    for spec in project.tools:
        if spec.source.kind == "genie_agent":
            names[spec.id] = [f"{spec.id}_{m}" for m in ("ask", "poll", "query_result")]
        else:
            names[spec.id] = [spec.id]
    return names


def check_agent_toml(
    project_root: Path,
    agent: BaseAgent,
    plan: AuthorizationPlan,
    config: AgentConfig | None,
    *,
    runtime_only: bool = False,
) -> list[Check]:
    """Findings for agent.toml vs the Python agent. One FAIL per mismatch."""
    from databricks_agentbricks.errors import AgentCliError

    from ._platform_tools import platform_tool_leaves

    try:
        project = load_agent_toml(project_root)
    except AgentCliError as exc:
        return [Check(_NAME, Status.FAIL, str(exc), "Fix agent.toml so `agentbricks` can load it.")]
    if project is None:
        return [Check(_NAME, Status.WARN, "no agent.toml; deploy generates one (deprecated)",
                      "Run `apx-agent agents migrate-agent-toml` to write an authored agent.toml.")]

    findings: list[Check] = []
    declared = platform_tool_names(project)
    leaves = platform_tool_leaves(agent)
    referenced: set[str] = set()
    for leaf, ref in leaves:
        unknown = sorted(ref.ids - declared.keys())
        if unknown:
            findings.append(Check(_NAME, Status.FAIL,
                                  f"{leaf._name}: platform_tools({', '.join(map(repr, unknown))}) not declared",
                                  "Declare the tool under [[tools]] in agent.toml, or remove the id."))
        ids = ref.ids or frozenset(declared)
        referenced |= ids
        python_names = {fn.__name__ for fn in leaf._tool_fns}
        clashes = sorted(python_names & {n for i in ids & declared.keys() for n in declared[i]})
        if clashes:
            findings.append(Check(_NAME, Status.FAIL,
                                  f"{leaf._name}: tool name collision {', '.join(clashes)}",
                                  "Rename the Python tool or the agent.toml tool id."))
    if runtime_only:
        return findings

    unreferenced = sorted(declared.keys() - referenced)
    if unreferenced:
        findings.append(Check(_NAME, Status.FAIL,
                              f"declared but unused: {', '.join(unreferenced)}",
                              "Reference them with platform_tools(...) on an agent, or remove them."))
    if str(project.framework) != "langgraph":
        findings.append(Check(_NAME, Status.FAIL, f"framework = {project.framework!s}",
                              'Set [agent] framework = "langgraph" (apx compiles to LangGraph).'))
    if str(project.server) != "agentbricks":
        findings.append(Check(_NAME, Status.FAIL, f"server = {project.server!s}",
                              'Set [agent] server = "agentbricks" for the durable target.'))
    missing = sorted(set(plan.user_api_scopes) - set(project.user_auth.additional_api_scopes))
    if missing:
        findings.append(Check(_NAME, Status.FAIL, f"user API scopes not declared: {', '.join(missing)}",
                              f"Add {missing} to [auth.user] additional_api_scopes in agent.toml."))
    needs_user = any(op.requires_request_context for op in plan.operations) or bool(plan.app_dependencies)
    if needs_user and not project.user_auth.required:
        findings.append(Check(_NAME, Status.FAIL, "tools need a request user but [auth.user] required is false",
                              "Set [auth.user] required = true in agent.toml."))
    findings.extend(_store_findings(project, config))
    return findings or [Check(_NAME, Status.OK, "agent.toml matches the Python agent")]


def _store_findings(project: Any, config: AgentConfig | None) -> list[Check]:
    if config is None:
        return []
    out: list[Check] = []
    pairs = (("memory", config.memory, project.memory_store, "agentbricks memory bind"),
             ("session", config.session, project.session_store, "agentbricks sessions bind"))
    for label, declared, bound, command in pairs:
        if declared is None or declared.type != "managed":
            continue
        if bound is None:
            out.append(Check(_NAME, Status.FAIL, f"managed {label} declared but no {label}_store bound",
                             f"Run `{command} <name>`."))
        elif declared.store_name is not None and declared.store_name != bound:
            out.append(Check(_NAME, Status.FAIL,
                             f"{label} store mismatch: pyproject {declared.store_name!r} vs agent.toml {bound!r}",
                             "Make [tool.apx.agent] and agent.toml name the same store."))
    return out
```

`AgentConfig` has no tracing field, so apx never names an experiment and the spec's tracing check (check 5) can never fire; it is not implemented, and Task 8 updates the spec to say so. The "`agent.toml` matches" OK row is returned only when there are no findings.

- [ ] **Step 4: Run to verify they pass**

Run: `cd python && uv run pytest tests/test_agent_toml.py -q`
Expected: all pass. For `test_missing_user_scope_fails`, if `compile_authorization_plan` yields no scope for a bare `UserClient`, use the dependency the existing `tests/test_apps_authorization.py` uses to produce a non-empty `user_api_scopes`.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_agent_toml.py python/tests/test_agent_toml.py
git commit -m "feat: check agent.toml against the Python agent"
```

---

### Task 3: Materialize platform tools through AgentKit

**Files:**
- Modify: `python/src/apx_agent/_platform_tools.py`
- Test: `python/tests/test_platform_tools.py`

**Interfaces:**
- Consumes: `load_agent_toml`, `platform_tool_names` (Task 2).
- Produces:
  - `ClientFor = Callable[[str], WorkspaceClient]` — `mode` ("user" | "app") → client.
  - `def static_platform_tools(project_root: Path, client_for: ClientFor) -> dict[str, list[BaseTool]]` — id → tools for `genie_agent` (any auth) and `auth = "app"` MCP kinds. Synchronous; MCP fetch run via the existing `_run_sync` helper in `mcp_consume.py`.
  - `async def request_platform_tools(project_root: Path, client_for: ClientFor, ids: frozenset[str]) -> dict[str, list[BaseTool]]` — id → tools for `auth = "user"` MCP kinds only.
  - `def _mcp_server_for(record: Any, client_for: ClientFor) -> Any` — the only use of the private `_server_from_tool`.

- [ ] **Step 1: Write the failing tests**

```python
# append to python/tests/test_platform_tools.py
from pathlib import Path
from types import SimpleNamespace

BASE = 'schema_version = 1\n\n[agent]\nframework = "langgraph"\nserver = "agentbricks"\n'
GENIE = '\n[[tools]]\nid = "pricing"\nauth = "user"\n[tools.source]\nkind = "genie_agent"\nspace_id = "01ef"\n'


def test_genie_tools_resolve_identity_per_call(tmp_path: Path, monkeypatch) -> None:
    from apx_agent._platform_tools import static_platform_tools

    (tmp_path / "agent.toml").write_text(BASE + GENIE)
    calls: list[str] = []

    def client_for(mode: str):
        calls.append(mode)
        return SimpleNamespace()

    tools = static_platform_tools(tmp_path, client_for)
    assert [t.name for t in tools["pricing"]] == ["pricing_ask", "pricing_poll", "pricing_query_result"]
    assert calls == []  # construction does no auth; GenieAgent calls client_for at call time


def test_project_root_is_pinned(tmp_path: Path, monkeypatch) -> None:
    import os

    from apx_agent._platform_tools import static_platform_tools

    (tmp_path / "agent.toml").write_text(BASE + GENIE)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    static_platform_tools(tmp_path, lambda mode: SimpleNamespace())
    assert "AGENTBRICKS_PROJECT_ROOT" not in os.environ  # pinned only for the load, then restored


def test_private_mcp_seam_still_exists() -> None:
    """Fails loudly when a databricks-agentbricks bump removes the one private symbol we use."""
    from databricks_agentkit.langgraph import mcp

    assert callable(mcp._server_from_tool)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_platform_tools.py -q`
Expected: FAIL — `ImportError: cannot import name 'static_platform_tools'`.

- [ ] **Step 3: Implement**

```python
# append to python/src/apx_agent/_platform_tools.py
import contextlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ClientFor = Callable[[str], Any]
_MCP_KINDS = frozenset({"mcp", "uc_function", "genie_one", "sandbox"})


@contextlib.contextmanager
def _pinned_root(project_root: Path) -> Iterator[None]:
    """AgentKit walks up from cwd for agent.toml; pin it to the project apx resolved."""
    previous = os.environ.get("AGENTBRICKS_PROJECT_ROOT")
    os.environ["AGENTBRICKS_PROJECT_ROOT"] = str(project_root.resolve())
    try:
        yield
    finally:
        if previous is None:
            del os.environ["AGENTBRICKS_PROJECT_ROOT"]
        else:
            os.environ["AGENTBRICKS_PROJECT_ROOT"] = previous


def _records(project_root: Path) -> list[Any]:
    from databricks_agentkit.runtime.tool_manifest import load_tools

    with _pinned_root(project_root):
        return list(load_tools(expected_framework="langgraph"))


def _mcp_server_for(record: Any, client_for: ClientFor) -> Any:
    # ponytail: the one private AgentKit import; pinned by test_private_mcp_seam_still_exists.
    # Upgrade path: a public per-record server builder in AgentKit.
    from databricks_agentkit.langgraph.mcp import _server_from_tool

    return _server_from_tool(record, workspace_client_for=client_for)


async def _fetch_mcp(records: list[Any], client_for: ClientFor) -> dict[str, list[Any]]:
    from databricks_agentkit.langgraph.mcp import mcp_client

    servers = [s for r in records if (s := _mcp_server_for(r, client_for)) is not None]
    if not servers:
        return {}
    client = mcp_client(servers, workspace_client_for=client_for, tools=tuple(records))
    return {s.name: await client.get_tools(server_name=s.name) for s in servers}


def static_platform_tools(project_root: Path, client_for: ClientFor) -> dict[str, list[Any]]:
    """Tools safe to build once: Genie (identity per call) and app-auth MCP."""
    from databricks_agentkit.langgraph.genie import genie_tools

    from .mcp_consume import _run_sync

    from ._agent_toml import load_agent_toml, platform_tool_names

    records = _records(project_root)
    owner = {name: tool_id
             for tool_id, names in platform_tool_names(load_agent_toml(project_root)).items()
             for name in names}
    out: dict[str, list[Any]] = {}
    with _pinned_root(project_root):
        for tool in genie_tools(workspace_client_for=client_for):
            out.setdefault(owner[tool.name], []).append(tool)
    app_mcp = [r for r in records if r.kind in _MCP_KINDS and (r.auth or "app") == "app"]
    out.update(_run_sync(_fetch_mcp(app_mcp, client_for)))
    unknown = sorted(r.id for r in records if r.kind not in _MCP_KINDS | {"genie_agent"})
    if unknown:
        raise ValueError(f"agent.toml tool kind not supported by platform_tools(): {unknown}")
    return out


async def request_platform_tools(project_root: Path, client_for: ClientFor,
                                 ids: frozenset[str]) -> dict[str, list[Any]]:
    """User-auth MCP tools, fetched inside the request so each caller gets their own client."""
    records = [r for r in _records(project_root)
               if r.kind in _MCP_KINDS and r.auth == "user" and (not ids or r.id in ids)]
    return await _fetch_mcp(records, client_for)
```

Genie tools are grouped by id through `platform_tool_names` (the same `<id>_ask` / `<id>_poll` / `<id>_query_result` names `genie_tools()` produces), so an id containing `_query` still groups correctly. Add a test with tool id `my_query` asserting all three tools land under `"my_query"`.

- [ ] **Step 4: Run to verify they pass**

Run: `cd python && uv run pytest tests/test_platform_tools.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_platform_tools.py python/tests/test_platform_tools.py
git commit -m "feat: materialize agent.toml tools through AgentKit loaders"
```

---

### Task 4: Compile platform tools into the graph, under apx guards

**Files:**
- Modify: `python/src/apx_agent/_compile.py:101-130` (`CompileContext`), `:397-405` (`_compile_llm_agent`)
- Modify: `python/src/apx_agent/_durable_agent.py:107-226` (`compile_durable_handlers`), `:230-308` (`compile_to_durable_agent_server`)
- Modify: `python/src/apx_agent/_runtime_targets.py:170-215` (`compile_agent`)
- Test: `python/tests/test_platform_tools.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `CompileContext.platform_tools: dict[str, list[BaseTool]]` (default empty) — id → tools.
  - `compile_to_langgraph(..., platform_tools: dict[str, list[BaseTool]] | None = None)`.
  - `compile_durable_handlers(..., project_root: Path | None = None)`; `compile_to_durable_agent_server(..., project_root: Path | None = None)`.

- [ ] **Step 1: Write the failing tests**

```python
# append to python/tests/test_platform_tools.py
import pytest


def _fake_tool(name: str):
    from langchain_core.tools import tool

    @tool(name)
    def _t(q: str) -> str:
        """Platform tool."""
        return f"platform:{q}"

    return _t


def test_platform_tool_runs_under_before_tool_guard(monkeypatch) -> None:
    from langchain_core.messages import HumanMessage

    from apx_agent import Agent, compile_to_langgraph

    seen: list[str] = []

    def guard(name: str, args: dict) -> None:
        seen.append(name)

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from apx_agent import _compile

    class _ToolFake(GenericFakeChatModel):  # same pattern as tests/test_a2a.py:396
        def bind_tools(self, tools, **kwargs):
            return self

    model = _ToolFake(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "pricing_ask", "args": {"q": "x"}, "id": "c1"}]),
        AIMessage(content="done"),
    ]))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **k: model)
    agent = Agent(name="a", tools=[*platform_tools("pricing")], before_tool=guard)
    graph = compile_to_langgraph(agent, ws=None, model="m",
                                 platform_tools={"pricing": [_fake_tool("pricing_ask")]})
    graph.invoke({"messages": [HumanMessage(content="go")]})
    assert seen == ["pricing_ask"]


def test_toolscope_on_platform_tool_is_rejected() -> None:
    from apx_agent._tool_scope import ToolScope, attach_scope

    [ref] = platform_tools("pricing")
    with pytest.raises(ValueError, match="downscope"):
        attach_scope(ref, ToolScope(catalogs=("sales",)))


def test_responses_target_rejects_platform_tools() -> None:
    from unittest.mock import MagicMock

    from apx_agent import Agent, compile_agent

    with pytest.raises(ValueError, match="durable_agent_server"):
        compile_agent(Agent(name="a", tools=[*platform_tools()]), model="m",
                      target="responses_agent", service_ws=MagicMock())


def test_user_tool_without_request_resolver_surfaces_sdk_error(tmp_path, monkeypatch) -> None:
    """Deployed (DATABRICKS_APP_NAME set) user tool with no resolver: AgentKit raises; apx adds no fallback."""
    from databricks_agentkit.runtime.auth import AuthError

    from apx_agent._platform_tools import _mcp_server_for

    monkeypatch.setenv("DATABRICKS_APP_NAME", "app")
    record = SimpleNamespace(kind="mcp", id="crm", auth="user", source=SimpleNamespace(service="x"))
    with pytest.raises(AuthError):
        _mcp_server_for(record, None)
```

Gating (approvals, `PolicyGate` ASK/DENY, watchdog) is a `before_tool` hook, so the guard test covers it. Budgets are token-based (`_budget.py` accrues model usage per turn), so platform tool calls fall under the same cap with no per-tool counter; there is nothing extra to wire.

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_platform_tools.py -q`
Expected: the four new tests FAIL (`compile_to_langgraph() got an unexpected keyword argument 'platform_tools'`, no rejection raised).

- [ ] **Step 3: Implement**

In `_tool_scope.attach_scope`, first line of the body:

```python
    from ._platform_tools import PlatformToolRef  # noqa: PLC0415

    if isinstance(fn, PlatformToolRef):
        raise ValueError("platform tools take their data ceiling from agent.toml downscope; ToolScope is not allowed")
```

In `CompileContext`, add the field:

```python
    platform_tools: dict[str, list[Any]] = field(default_factory=dict)
    """agent.toml tools by id, already materialized (see _platform_tools)."""
```

In `_compile_llm_agent`, after `tools = [...]` for `author_fns`:

```python
    for ref in agent._platform_tool_refs:
        ids = ref.ids or frozenset(ctx.platform_tools)
        tools.extend(t for i in sorted(ids) for t in ctx.platform_tools.get(i, []))
```

Thread `platform_tools` from `compile_to_langgraph`'s new keyword into the `CompileContext` it builds.

In `compile_agent` (`_runtime_targets.py`), before `report = inspect_target(...)`:

```python
    from ._platform_tools import platform_tool_leaves

    if platform_tool_leaves(agent) and target != "durable_agent_server":
        raise ValueError("platform_tools() requires target='durable_agent_server' (agent.toml tools are Agent Bricks tools)")
```

In `compile_to_durable_agent_server`, take `project_root: Path | None = None` (launcher passes `Path.cwd()`), then:

```python
    static: dict[str, list[Any]] = {}
    platform_error: str | None = None
    if project_root is not None and platform_tool_leaves(agent):
        from ._agent_toml import check_agent_toml
        from ._apps_authorization import compile_authorization_plan
        from ._platform_tools import static_platform_tools

        fails = [c for c in check_agent_toml(project_root, agent,
                                             compile_authorization_plan(agent, model=model), None,
                                             runtime_only=True) if c.status.value == "fail"]
        if fails:
            raise ValueError("; ".join(f"{c.detail} — {c.fix}" for c in fails))
        try:
            static = static_platform_tools(project_root, lambda mode: service_ws)
        except Exception as exc:  # surfaced on /readyz, never swallowed
            platform_error = f"{type(exc).__name__}: {exc}"
```

Pass `static` and `project_root` into `compile_durable_handlers`. In the existing `/readyz` handler, before the success return:

```python
        if platform_error is not None:
            return JSONResponse(status_code=503, content={
                "status": "degraded", "checks": {"platform_tools": platform_error},
            })
```

In `compile_durable_handlers.invoke`, where `user_ws` is resolved, build the per-request map and pass it:

```python
        platform = dict(static)
        if project_root is not None:
            refs = [ref for _, ref in platform_tool_leaves(agent)]
            if refs:
                from ._platform_tools import request_platform_tools

                client_for = lambda mode: (context.request_auth.client_for("user")
                                           if mode == "user" else service_ws)
                ids = frozenset().union(*(r.ids for r in refs))
                platform.update(await request_platform_tools(project_root, client_for, ids))
        graph = compile_to_langgraph(..., platform_tools=platform)
```

Genie tools built at startup get `client_for` that reads the *current* request: replace the startup `lambda mode: service_ws` for Genie with a resolver that reads a `contextvars.ContextVar` the invoke sets to `context.request_auth` for the duration of the turn, falling back to `service_ws` only for `mode == "app"`. A `mode == "user"` lookup with the var unset raises `RuntimeError("platform tool needs a request user")`.

`compile_agent` passes `project_root` through; `_serve.create_app` and the generated `app.py` (`_project_gen.py:255-268`) pass `project_root=Path.cwd()`.

- [ ] **Step 4: Run to verify they pass**

Run: `cd python && uv run pytest tests/test_platform_tools.py tests/test_runtime_targets.py tests/test_durable_recovery.py -q`
Expected: all pass; existing durable tests unchanged.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_compile.py python/src/apx_agent/_durable_agent.py python/src/apx_agent/_runtime_targets.py python/src/apx_agent/_serve.py python/src/apx_agent/_project_gen.py python/tests/test_platform_tools.py
git commit -m "feat: compile agent.toml tools into the graph under apx guards"
```

---

### Task 5: Deploy stages the authored `agent.toml`

**Files:**
- Modify: `python/src/apx_agent/_agent_toml.py` (add `stage_agent_toml`)
- Modify: `python/src/apx_agent/cli.py:10906-10921`
- Modify: `python/src/apx_agent/_apps_api_deploy.py:122-138` (`_stage`)
- Test: `python/tests/test_agent_toml.py`

**Interfaces:**
- Produces: `def stage_agent_toml(project_root: Path, staged_source: Path, generate: Callable[[], str], log: Callable[[str], None]) -> Path` — copies the authored file byte for byte, or writes `generate()` and logs the deprecation; refuses symlinks; returns the staged path.

- [ ] **Step 1: Write the failing tests**

```python
# append to python/tests/test_agent_toml.py
from apx_agent._agent_toml import stage_agent_toml

GENERATED = "# Generated by APX from agent declarations; do not edit.\nschema_version = 1\n"


def test_authored_file_staged_byte_for_byte_replacing_stale_generated(tmp_path: Path) -> None:
    (tmp_path / "agent.toml").write_text(BASE + GENIE)
    build = tmp_path / ".build"
    build.mkdir()
    (build / "agent.toml").write_text(GENERATED)  # stale output from an older deploy
    logs: list[str] = []
    staged = stage_agent_toml(tmp_path, build, lambda: GENERATED, logs.append)
    assert staged.read_bytes() == (tmp_path / "agent.toml").read_bytes()
    assert not any("deprecated" in line for line in logs)


def test_no_authored_file_generates_and_warns(tmp_path: Path) -> None:
    build = tmp_path / ".build"
    build.mkdir()
    logs: list[str] = []
    staged = stage_agent_toml(tmp_path, build, lambda: GENERATED, logs.append)
    assert staged.read_text() == GENERATED
    assert any("migrate-agent-toml" in line for line in logs)


def test_symlinked_manifest_refused(tmp_path: Path) -> None:
    (tmp_path / "real.toml").write_text(BASE)
    (tmp_path / "agent.toml").symlink_to(tmp_path / "real.toml")
    (tmp_path / ".build").mkdir()
    with pytest.raises(ValueError, match="symlink"):
        stage_agent_toml(tmp_path, tmp_path / ".build", lambda: GENERATED, print)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd python && uv run pytest tests/test_agent_toml.py -q -k stage or symlink or generates`
Expected: FAIL — `ImportError: cannot import name 'stage_agent_toml'`.

- [ ] **Step 3: Implement**

```python
# append to python/src/apx_agent/_agent_toml.py
import shutil
from collections.abc import Callable


def stage_agent_toml(project_root: Path, staged_source: Path,
                     generate: Callable[[], str], log: Callable[[str], None]) -> Path:
    """Stage the authored agent.toml unchanged; generate (deprecated) only when absent."""
    authored = project_root / _NAME
    target = staged_source / _NAME
    if authored.is_symlink() or target.is_symlink():
        raise ValueError("Refusing a symlinked agent.toml.")
    if authored.is_file():
        shutil.copyfile(authored, target)
        log(f"  staged authored agent.toml: {target}")
        return target
    target.write_text(generate())
    log("  no agent.toml: generated one (deprecated) — run `apx-agent agents migrate-agent-toml`")
    return target
```

In `cli.py` replace the block at 10906–10921 (from `manifest = source / "agent.toml"` through the `log(...)`) with:

```python
            from ._agent_toml import stage_agent_toml

            if source.is_symlink():
                raise click.ClickException("Refusing to write a symlinked native agent manifest.")
            if not source.is_dir() or not source.resolve().is_relative_to(cwd.resolve()) or source.resolve() == cwd.resolve():
                raise click.ClickException("The durable target requires a staged source directory inside the project.")
            try:
                stage_agent_toml(cwd, source, lambda: build_native_manifest(
                    config=effective_config, authorization_plan=authorization_plan, agent=agent), log)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
```

In `_apps_api_deploy._stage`, drop `"agent.toml"` from the root-file refusal loop (keep `app.yaml`, `app.yml`) and replace the manifest block with:

```python
    from ._agent_toml import stage_agent_toml

    try:
        stage_agent_toml(cwd, source, lambda: build_native_manifest(config=config, authorization_plan=plan, agent=agent), log)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
```

Deploy preflight: in `cli.py`, directly after the `authorization_plan = compile_authorization_plan(...)` at line 10739 and before any workspace call, add:

```python
        if effective_config is not None and effective_config.target == "durable_agent_server":
            from ._agent_toml import check_agent_toml

            fails = [c for c in check_agent_toml(cwd, agent, authorization_plan, effective_config)
                     if c.status is _doctor_mod.Status.FAIL]
            if fails:
                raise click.ClickException("agent.toml: " + "; ".join(f"{c.detail} — {c.fix}" for c in fails))
```

Read the surrounding lines to confirm `agent` and `_doctor_mod` are in scope at that point; if `agent` is resolved later, move this block to just after it, still before the first workspace call.

- [ ] **Step 4: Run to verify they pass**

Run: `cd python && uv run pytest tests/test_agent_toml.py tests/test_apps_api_deploy.py tests/test_deploy_apps.py -q`
Expected: all pass. Any existing test asserting "refusing to ignore an existing root manifest" for `agent.toml` must be updated to assert the new staging instead — that behavior change is the point of this task; say so in the PR.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_agent_toml.py python/src/apx_agent/cli.py python/src/apx_agent/_apps_api_deploy.py python/tests
git commit -m "feat: deploy stages the authored agent.toml and checks it first"
```

---

### Task 6: Doctor rows

**Files:**
- Modify: `python/src/apx_agent/_doctor.py:196-243` (`run_checks`)
- Modify: `python/src/apx_agent/_doctor_durable.py:602-628` (`check_durable_readiness`)
- Test: `python/tests/test_agent_toml.py`

**Interfaces:**
- Consumes: `check_agent_toml` (Task 2).

- [ ] **Step 1: Write the failing test**

```python
# append to python/tests/test_agent_toml.py
def test_doctor_project_group_reports_agent_toml(tmp_path: Path, monkeypatch) -> None:
    from apx_agent._doctor import run_checks

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\n[tool.apx.agent]\nname = "p"\nmodel = "m"\ntarget = "durable_agent_server"\n')
    (tmp_path / "agent.py").write_text(
        "from apx_agent import Agent, platform_tools\nagent = Agent(name='p', tools=[*platform_tools('nope')])\n")
    (tmp_path / "agent.toml").write_text(BASE)
    monkeypatch.chdir(tmp_path)
    groups = dict(run_checks(tmp_path, online=False))
    rows = [c for c in groups["Project"] if c.name == "agent.toml"]
    assert rows and rows[0].status is Status.FAIL and "nope" in rows[0].detail
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_agent_toml.py -q -k doctor`
Expected: FAIL — no `agent.toml` row.

- [ ] **Step 3: Implement**

In `run_checks`, inside the `else:` branch after `check_agent_image`, add:

```python
        if agent_config is not None and agent_config.target == "durable_agent_server":
            project.extend(_agent_toml_rows(cwd, agent_config))
```

and add to `_doctor.py`:

```python
def _agent_toml_rows(cwd: Path, config: AgentConfig) -> list[Check]:
    from ._agent_toml import check_agent_toml
    from ._apps_authorization import compile_authorization_plan
    from ._wiring import resolve_agent

    try:
        agent = resolve_agent("agent:agent", config)
    except Exception as exc:  # reported, not raised: doctor keeps going
        return [Check("agent.toml", Status.SKIP, f"agent not importable: {type(exc).__name__}")]
    return check_agent_toml(cwd, agent, compile_authorization_plan(agent, model=config.model), config)
```

In `check_durable_readiness`, after `results.append(Check("Config", ...))`, add the same rows as an advisory stage:

```python
        from ._agent_toml import check_agent_toml
        from ._apps_authorization import compile_authorization_plan

        results.extend(c for c in check_agent_toml(project, loaded.agent,
                       compile_authorization_plan(loaded.agent, model=loaded.config.model), loaded.config)
                       if c.status is not Status.OK)
```

Confirm `_cascade` treats an extra FAIL row as blocking later stages; if it does and that hides useful Startup output, append these rows after `_cascade(...)` instead, alongside `_advisory(...)`.

- [ ] **Step 4: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_agent_toml.py tests/test_doctor.py tests/test_doctor_durable.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_doctor.py python/src/apx_agent/_doctor_durable.py python/tests/test_agent_toml.py
git commit -m "feat: doctor reports agent.toml mismatches"
```

---

### Task 7: `apx-agent agents migrate-agent-toml`

**Files:**
- Modify: `python/src/apx_agent/cli.py` (next to `agents migrate-to-okf`, ~line 4871)
- Modify: `python/src/apx_agent/_agent_toml.py` (add `write_agent_toml`)
- Test: `python/tests/test_agent_toml_reality_ctk.py`

**Interfaces:**
- Produces: `def write_agent_toml(project_root: Path, config: AgentConfig, plan: AuthorizationPlan) -> Path` — raises `FileExistsError` when `agent.toml` exists.

- [ ] **Step 1: Write the failing reality test**

```python
# python/tests/test_agent_toml_reality_ctk.py
from __future__ import annotations

from pathlib import Path

import pytest
from ctk import Artifact, verify

from apx_agent._agent_toml import load_agent_toml, write_agent_toml
from apx_agent._apps_authorization import AuthorizationPlan
from apx_agent._models import AgentConfig


def _plan(scopes: tuple[str, ...]) -> AuthorizationPlan:
    return AuthorizationPlan(operations=(), user_resources=(), service_resources=(),
                             user_api_scopes=scopes, app_dependencies=())


def test_migrate_round_trips_through_the_sdk(tmp_path: Path) -> None:
    config = AgentConfig.model_validate({
        "name": "p", "model": "m", "target": "durable_agent_server",
        "memory": {"type": "managed", "store_name": "mem"},
    })
    path = write_agent_toml(tmp_path, config, _plan(("sql",)))
    verify(Artifact(str(path), min_bytes=60, must_contain="[memory_store]"))
    project = load_agent_toml(tmp_path)
    assert project.memory_store == "mem"
    assert project.user_auth.additional_api_scopes == ("sql",)


def test_second_run_refuses_and_leaves_file_untouched(tmp_path: Path) -> None:
    config = AgentConfig.model_validate({"name": "p", "model": "m", "target": "durable_agent_server"})
    path = write_agent_toml(tmp_path, config, _plan(()))
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_agent_toml(tmp_path, config, _plan(("sql",)))
    assert path.read_bytes() == before
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_agent_toml_reality_ctk.py -q`
Expected: FAIL — `ImportError: cannot import name 'write_agent_toml'`.

- [ ] **Step 3: Implement**

```python
# append to python/src/apx_agent/_agent_toml.py
def write_agent_toml(project_root: Path, config: AgentConfig, plan: AuthorizationPlan) -> Path:
    """One-shot migration: the SDK writes the file; apx adds [auth.user]."""
    import tomlkit
    from databricks_agentbricks.projects.agent_project import AgentProject

    path = project_root / _NAME
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"{path} already exists; agent.toml is not overwritten")
    managed = lambda c: c.store_name if c is not None and c.type == "managed" else None
    project = AgentProject.create(project_root, framework="langgraph", server="agentbricks",
                                  memory_store=managed(config.memory), session_store=managed(config.session))
    project.write()
    needs_user = any(op.requires_request_context for op in plan.operations) or bool(plan.app_dependencies)
    if needs_user or plan.user_api_scopes:
        # AgentProject has no [auth.user] setter (spec §Migration command).
        document = tomlkit.parse(path.read_text())
        user = tomlkit.table()
        user.add("required", needs_user or config.memory is not None)
        user.add("additional_api_scopes", list(plan.user_api_scopes))
        auth = tomlkit.table()
        auth.add("user", user)
        document.add("auth", auth)
        path.write_text(tomlkit.dumps(document))
    load_agent_toml(project_root)  # read-after-write: the SDK must accept what we wrote
    return path
```

The `required` rule mirrors `build_native_manifest` (`_durable_agent.py:52-56`) so a migrated project produces the same `[auth.user]` it gets today. Add the click command next to `migrate-to-okf`:

```python
@agents.command("migrate-agent-toml")
def migrate_agent_toml() -> None:
    """Write an authored agent.toml from [tool.apx.agent] (one time; never overwrites)."""
    from ._agent_toml import write_agent_toml
    from ._apps_authorization import compile_authorization_plan
    from ._inspection import _load_agent_config
    from ._wiring import resolve_agent

    cwd = Path.cwd()
    config = _load_agent_config(pyproject_path=cwd / "pyproject.toml")
    if config is None:
        raise click.ClickException("No [tool.apx.agent] to migrate.")
    agent = resolve_agent("agent:agent", config)
    try:
        path = write_agent_toml(cwd, config, compile_authorization_plan(agent, model=config.model))
    except FileExistsError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"wrote {path}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_agent_toml_reality_ctk.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_agent_toml.py python/src/apx_agent/cli.py python/tests/test_agent_toml_reality_ctk.py
git commit -m "feat: agents migrate-agent-toml writes agent.toml through the SDK"
```

---

### Task 8: Staging reality check, capabilities, docs

**Files:**
- Modify: `python/tests/test_agent_toml_reality_ctk.py`
- Modify: `python/capabilities.yaml` (local, git-excluded — via `caps add`)
- Modify: `docs/running/runtime-targets.md`
- Modify: `docs/design/agent-toml-source-of-truth.md` (status line only)

- [ ] **Step 1: Write the staging reality test**

```python
# append to python/tests/test_agent_toml_reality_ctk.py
from apx_agent._agent_toml import stage_agent_toml


def test_staging_a_real_example_ships_the_authored_file(tmp_path: Path) -> None:
    import shutil

    src = Path(__file__).parents[1] / "examples" / "data-triage-agent"
    project = tmp_path / "p"
    shutil.copytree(src, project, ignore=shutil.ignore_patterns(".venv", ".build", "__pycache__"))
    authored = 'schema_version = 1\n\n[agent]\nframework = "langgraph"\nserver = "agentbricks"\n\n[session_store]\nname = "s"\n'
    (project / "agent.toml").write_text(authored)
    (project / ".build").mkdir()
    staged = stage_agent_toml(project, project / ".build", lambda: "generated", print)
    verify(Artifact(str(staged), min_bytes=40, must_contain='[session_store]'))
    assert staged.read_text() == authored
```

- [ ] **Step 2: Run it**

Run: `cd python && uv run pytest tests/test_agent_toml_reality_ctk.py -q`
Expected: 3 passed (Task 7's two plus this one).

- [ ] **Step 3: Register capabilities**

```bash
cd python
PYTHONPATH=.ctk uv run python -m caps add --id agent-toml-mismatch-fails-preflight --tier cheap \
  --description "a scope missing from agent.toml fails deploy before any workspace change" \
  --given "an agent.toml without a scope a Python tool needs" --when "check_agent_toml runs" \
  --then "a FAIL finding names the scope and the fix" \
  --deps src/apx_agent/_agent_toml.py --check tests/test_agent_toml.py::test_missing_user_scope_fails
PYTHONPATH=.ctk uv run python -m caps verify --capability agent-toml-mismatch-fails-preflight
```

Expected: `agent-toml-mismatch-fails-preflight: pass`. The live capability `platform-tool-runs-under-apx-guards-live` needs a deployed durable agent with a Genie space on fevm; add it with `caps add --tier live --check { shell: ... }` only when that deploy is done, and say in the PR that it is not yet proven.

- [ ] **Step 4: Document**

In `docs/running/runtime-targets.md`, add a section "agent.toml and platform tools" stating: Agent Bricks settings live in `agent.toml`; apx reads and checks it, stages it unchanged, and generates one only when absent (deprecated); `platform_tools(*ids)` usage with one code example; identity rules (Genie per call, user MCP per request, app MCP at startup); every mismatch fails deploy and doctor; `apx-agent agents migrate-agent-toml`. In the spec: change the status line to `**Status:** implemented (sub-project 1) · **Date:** 2026-10-19 · **Sub-project:** 1 of 4`; rename the migration command to `apx-agent agents migrate-agent-toml` (matching the existing `agents migrate-to-okf`); and replace check 5 (tracing) with a note that `AgentConfig` has no tracing field, so there is nothing to compare.

- [ ] **Step 5: Full gate and commit**

Run: `make check && cd python && uv run pyright && cd .. && pre-commit run --files $(git diff --name-only main...HEAD)`
Expected: all green.

```bash
git add python/tests/test_agent_toml_reality_ctk.py docs/running/runtime-targets.md docs/design/agent-toml-source-of-truth.md
git commit -m "docs: agent.toml and platform tools; reality checks"
```
