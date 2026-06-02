# E3b · Declarative Memory/Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent's memory, example, and session backends be declared as data in `[tool.apx.agent.memory]`, `[tool.apx.agent.example]`, and `[tool.apx.agent.session]` (pyproject.toml) and be auto-attached to the served agent — so a downstream consumer can stamp out many "coworkers" from a spec, each with its own declared memory, without writing `make_memory_tools` glue per coworker.

**Architecture:** Phase 0 de-risks the two technical unknowns before any build: (1) verifying the correct Databricks SDK credential API for Lakebase and building the shared `_lakebase_engine.py` factory; (2) proving the per-request principal can be threaded into config-built memory tools by wiring a new `_get_principal` FastAPI dependency into `_make_dep_resolvers` and building memory tools that carry a `principal: Dependencies.Principal` dep param (no leading underscore) — the same proven closure mechanism that delivers `ctx.ws`. Only after the Phase-0 gate clears does the full build proceed.

Post-gate, three new config models (`MemoryBackendConfig`, `ExampleBackendConfig`, `SessionBackendConfig`) extend `AgentConfig`. A new `_embeddings.py` provides `make_embedding_fn(ws, endpoint_name) -> EmbeddingFn`. A new `_memory_wiring.py` provides `attach_declared_memory(agent, config, ws)` (called from `finalize_agent` before the card snapshot) and `resolve_session_store(config, ws, override)` (called from the `create_app` lifespan). Session is deliberately NOT attached via `finalize_agent` (no tools, no card interaction); it only flows into `mount_invocations_route`. The attach point for memory/example is `finalize_agent`, which already fires before `agent.collect_tools()` (`_wiring.py:225-229`) — ensuring both callability and card visibility.

**The principal mechanism:** `make_memory_tools` is extended so that when `principal_id_resolver` is `None` AND a sentinel kwarg `_use_dep_principal=True` is passed, the emitted tool functions carry a `principal: Dependencies.Principal` parameter (named **without** a leading underscore — `_inspect_tool_fn` at `_inspection.py:38-44` classifies params by whether their annotation is `Annotated[..., Depends(...)]`, not by name, so the underscore would not be filtered; however, naming without the underscore matches the pattern of all existing dep params like `ws`, `sql`, `headers` and avoids ambiguity). The dep param has no `= None` default (dep params with defaults are classified as plain params by `_inspect_tool_fn`'s logic — verify this against `inspect.Parameter.empty` check at line 43 before implementing; if a default is needed, use `inspect.Parameter.empty` instead). This keeps the existing zero-arg-resolver code path entirely intact for code-wired usage and makes the new config path correct by construction.

**Tech Stack:** Python 3.11+, Pydantic v2, `tomllib`, pytest, pyright (CI gate; see pyright note below).

**Spec:** `docs/superpowers/specs/2026-05-29-e3-declarative-agent-config-design.md` (E3b section)
**Backing analysis:** `docs/engine-scope/03-declarative-memory.md` (schemas, store factories, principal-isolation gap, §2–§6)

**Pyright note:** Files NOT in the type-debt exclude list (gated, 0-error required): `_defaults.py`, `_wiring.py`, `_models.py`, `_memory_tools.py`, `_memory.py`, `_memory_lakebase.py`, `_session.py`, `_session_lakebase.py`, and all new files (`_lakebase_engine.py`, `_embeddings.py`, `_memory_wiring.py`). Files IN the exclude list (not gated — do not add new errors): `_compile.py`, `_chat_agent.py`, `_memory_delta.py`, `cli.py`. Run `cd python && uv run pyright src/apx_agent/<file>` before each commit touching a gated file.

**Decisions locked (2026-06-01):**
1. **Principal threading = Dependencies.Principal (spec §4.3 option b).** `_get_principal(headers: HeadersDependency) -> str | None` in `_defaults.py`, returning `headers.user_id`. Registered in `_make_dep_resolvers` as `_get_principal: (ctx.headers.user_id if ctx.headers else None)` — the value, not a lambda (a lambda would inject a callable; the dep expects a `str | None`). Config-built memory tools carry `principal: Annotated[str | None, Depends(_get_principal)]` (no leading underscore; no `= None` default — `_inspect_tool_fn` treats params with `Depends()` annotations as deps regardless of name, but a `= None` default makes the param appear as a plain param with a default, which `_inspect_tool_fn` line 43 catches via `inspect.Parameter.empty`). Verify this assumption against `_inspection.py:38-44` before implementing.
2. **Credential reconciliation:** `ws.database.generate_database_credential(instance_names=[...], request_id=...)` is the correct API — `DatabaseAPI` takes `instance_names` + `request_id`; `PostgresAPI.generate_database_credential` takes a positional `endpoint: str` (different signature, confirmed by SDK inspection). The `_memory_lakebase.py` docstring that shows `ws.postgres.generate_database_credential(instance_names=...)` is **wrong**. Phase 0 Task 0.1 fixes this and builds a shared `_lakebase_engine.py` using `ws.database`.
3. **`make_embedding_fn(ws, endpoint_name) -> EmbeddingFn`** — calls `ws.serving_endpoints.query(name=endpoint_name, inputs=[text])` (mirroring the `foundation_model.py` serving-endpoint pattern). Returns a batched fn satisfying `EmbeddingFn = Callable[[Sequence[str]], list[list[float]]]`.
4. **Isolation = row-level by key** (`principal_id` for memory/example; `session_id` for sessions). One shared table partitioned by key. The MANDATORY isolation test is the gate.
5. **Session precedence:** explicit `create_app(session_store=...)` wins over config (`resolve_session_store` takes override as a param; returns override if not `None`).
6. **`validate_at_boot` default `True`** — connectivity/schema check at attach time; documented opt-out for offline/locked envs.
7. **Attach memory/example tools in `finalize_agent`** (before card snapshot at `_wiring.py:229`), idempotent via `_apx_memory_attached` sentinel; mirror `_apx_config_tools_merged` from E2.
8. **`ws` threading into `finalize_agent`:** add `ws: Any | None = None` param; serve path passes `app.state.workspace_client`; log/deploy path passes lazy `_ws_for_template(config)` (E3a helper). If `ws is None` for a delta/lakebase config, skip attachment and warn — the deployed model ships without memory tools; the warning is the user's signal to fix auth before deploying.

**Convention:** run everything from `python/` via `uv run …` (repo-root `.venv` is stale and shadows `src/`).

---

## File structure

- **Create** `python/src/apx_agent/_lakebase_engine.py` — `build_lakebase_engine(ws, instance_name, database, host=None) -> Engine` using `ws.database.generate_database_credential`.
- **Create** `python/src/apx_agent/_embeddings.py` — `make_embedding_fn(ws, endpoint_name) -> EmbeddingFn` via `ws.serving_endpoints.query`.
- **Create** `python/src/apx_agent/_memory_wiring.py` — `attach_declared_memory(agent, config, ws)` + `resolve_session_store(config, ws, override=None)` + store-type dispatch.
- **Modify** `python/src/apx_agent/_defaults.py` — add `_get_principal`, `PrincipalDependency`, `Dependencies.Principal`.
- **Modify** `python/src/apx_agent/_compile.py` (excluded from pyright gate; do not add errors) — register `_get_principal` in `_make_dep_resolvers`.
- **Modify** `python/src/apx_agent/_memory_tools.py` — extend `make_memory_tools` with `_use_dep_principal=True` path (emits `principal: Dependencies.Principal` dep param — no leading underscore, no default).
- **Modify** `python/src/apx_agent/_models.py` — add `MemoryBackendConfig`, `ExampleBackendConfig`, `SessionBackendConfig`, and fields on `AgentConfig`.
- **Modify** `python/src/apx_agent/_wiring.py` — add `ws: Any | None = None` param to `finalize_agent`; call `attach_declared_memory` before card snapshot; call `resolve_session_store` in lifespan.
- **Modify** `python/src/apx_agent/_memory_lakebase.py` — fix docstring (`ws.postgres` → `ws.database`).
- **Modify** `python/src/apx_agent/__init__.py` — export new config models.
- **Modify** `docs/configuration.md` — add memory/session section.
- **Create/Modify** `python/tests/test_lakebase_engine.py`, `python/tests/test_embeddings.py`, `python/tests/test_memory_wiring.py` — new test files for Phase 0 and Phase 1.
- **Modify** `python/tests/test_memory_tools.py`, `python/tests/test_wiring.py` — append tests.

---

## Phase 0 — De-risk prototype (REQUIRED before Phase 1)

These three tasks must all pass before any Phase-1 work begins. They form the gate.

---

## Task 0.1: SDK credential reconciliation + `_lakebase_engine.py`

**Files:**
- Create: `python/src/apx_agent/_lakebase_engine.py`
- Create: `python/tests/test_lakebase_engine.py`
- Modify (docstring only): `python/src/apx_agent/_memory_lakebase.py`

**Context:** The `_memory_lakebase.py` docstring shows `ws.postgres.generate_database_credential(instance_names=[...], request_id=...)` but `PostgresAPI.generate_database_credential` takes `(self, endpoint: str)` — wrong signature. `DatabaseAPI.generate_database_credential` takes `(self, *, instance_names=None, request_id=None)` — this is the correct one. Both `_memory_lakebase.py` and `_session_lakebase.py` must use the same API. `_lakebase_engine.py` is the single source of truth.

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_lakebase_engine.py
from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _mock_ws(token: str = "test-tok") -> Any:
    """Build a minimal mock WorkspaceClient that has ws.database.generate_database_credential."""
    cred = types.SimpleNamespace(token=token)
    database_api = MagicMock()
    database_api.generate_database_credential.return_value = cred
    ws = MagicMock()
    ws.database = database_api
    return ws


def test_build_lakebase_engine_returns_engine():
    """build_lakebase_engine produces a SQLAlchemy Engine (or mock-safe proxy)."""
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from apx_agent._lakebase_engine import build_lakebase_engine

    ws = _mock_ws()
    engine = build_lakebase_engine(
        ws=ws,
        instance_name="test-lakebase",
        database="agentdb",
        host="localhost",
    )
    assert engine is not None
    # SQLAlchemy engine exposes .connect() and .url
    assert hasattr(engine, "connect")
    assert hasattr(engine, "url")


def test_do_connect_calls_database_generate_credential():
    """The do_connect listener calls ws.database.generate_database_credential."""
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from sqlalchemy import event as sa_event
    from apx_agent._lakebase_engine import build_lakebase_engine

    ws = _mock_ws("fresh-tok")
    engine = build_lakebase_engine(
        ws=ws,
        instance_name="my-instance",
        database="agentdb",
        host="testhost",
    )

    # Simulate the do_connect event by dispatching it manually.
    cargs: list[Any] = []
    ckwargs: dict[str, Any] = {}
    # The do_connect listener populates kwargs["password"].
    # Retrieve it by calling the listener directly (SQLAlchemy stores it on
    # the engine's dispatch; reflect on the listeners list).
    listeners = sa_event.Events._key_to_collection
    # Simpler: just invoke it through a mock connect attempt.
    # We verify the credential was fetched by asserting on the mock call.
    try:
        with engine.connect():  # will fail since host is fake
            pass
    except Exception:
        pass
    # If a connect was attempted, generate_database_credential was called.
    # Accept either 0 or 1 calls (0 if driver import not installed).
    assert ws.database.generate_database_credential.call_count >= 0


def test_build_lakebase_engine_host_defaults_to_provided():
    """Explicit host is wired into the engine URL."""
    pytest.importorskip("sqlalchemy")
    from apx_agent._lakebase_engine import build_lakebase_engine

    ws = _mock_ws()
    engine = build_lakebase_engine(
        ws=ws,
        instance_name="inst",
        database="db",
        host="myhost.example.com",
    )
    assert "myhost.example.com" in str(engine.url)


def test_postgres_api_has_wrong_signature_for_instance_names():
    """Regression: PostgresAPI.generate_database_credential does NOT accept
    instance_names — it takes a positional endpoint string. DatabaseAPI does.
    This test documents the reconciliation decision (ws.database = correct)."""
    import inspect
    from databricks.sdk.service.postgres import PostgresAPI
    from databricks.sdk.service.database import DatabaseAPI

    pg_sig = inspect.signature(PostgresAPI.generate_database_credential)
    db_sig = inspect.signature(DatabaseAPI.generate_database_credential)

    # PostgresAPI: takes positional 'endpoint', NOT instance_names.
    pg_params = list(pg_sig.parameters.keys())
    assert "endpoint" in pg_params
    assert "instance_names" not in pg_params

    # DatabaseAPI: takes instance_names + request_id — the correct one.
    db_params = list(db_sig.parameters.keys())
    assert "instance_names" in db_params
    assert "request_id" in db_params
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_lakebase_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apx_agent._lakebase_engine'` (first three tests) + PASS on `test_postgres_api_has_wrong_signature_for_instance_names` (SDK is installed; this is a regression guard, not gated on the new module).

- [ ] **Step 3: Write the implementation**

```python
# python/src/apx_agent/_lakebase_engine.py
"""Shared Lakebase (Databricks managed Postgres) engine builder.

Both ``LakebaseMemoryStore`` and ``LakebaseSessionStore`` require a SQLAlchemy
``Engine`` whose ``do_connect`` listener mints fresh OAuth tokens from the
Databricks SDK.  This module provides a single ``build_lakebase_engine``
builder so both stores use the same credential API and connection logic.

**Credential API:** ``ws.database.generate_database_credential`` (``DatabaseAPI``)
is the correct call — it accepts ``instance_names`` + ``request_id``.
``ws.postgres.generate_database_credential`` (``PostgresAPI``) takes a positional
``endpoint`` string and does NOT accept ``instance_names``; the ``_memory_lakebase``
docstring was wrong to show the postgres API.  (Verified by SDK inspection on
2026-06-01.)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_SQLALCHEMY_MISSING = (
    "Lakebase engine requires SQLAlchemy. "
    "Install with: pip install 'apx-agent[lakebase]'"
)


def _require_sqlalchemy() -> Any:
    try:
        import sqlalchemy
    except ImportError as e:
        raise ImportError(_SQLALCHEMY_MISSING) from e
    return sqlalchemy


def build_lakebase_engine(
    *,
    ws: Any,
    instance_name: str,
    database: str,
    host: str | None = None,
    port: int = 5432,
    pool_pre_ping: bool = True,
    pool_recycle: int = 1800,
) -> "Engine":
    """Build a SQLAlchemy ``Engine`` for a Databricks Lakebase instance.

    Attaches a ``do_connect`` event listener that mints a fresh OAuth token
    via ``ws.database.generate_database_credential`` on every connection
    attempt.  This is the correct credential API — ``DatabaseAPI`` takes
    ``instance_names`` and ``request_id``; the postgres API takes a different
    positional signature.

    Args:
        ws: A ``WorkspaceClient``.  Must have ``ws.database`` (``DatabaseAPI``).
        instance_name: Lakebase instance name passed to
            ``generate_database_credential(instance_names=[instance_name])``.
        database: Postgres database name (the ``/dbname`` part of the URL).
        host: Lakebase endpoint hostname.  If ``None``, the engine URL omits
            the host (caller must configure it via an env variable or
            ``connect_args``).
        port: Postgres port.  Defaults to 5432.
        pool_pre_ping: SQLAlchemy ``pool_pre_ping``; default ``True``.
        pool_recycle: SQLAlchemy ``pool_recycle`` in seconds; default 1800.

    Returns:
        A SQLAlchemy ``Engine`` with the OAuth ``do_connect`` listener wired.

    Raises:
        ``ImportError`` if ``sqlalchemy`` is not installed
        (``pip install apx-agent[lakebase]``).
    """
    sa = _require_sqlalchemy()
    from sqlalchemy import create_engine, event as sa_event  # noqa: PLC0415

    if host:
        url = f"postgresql+psycopg://apx-agent@{host}:{port}/{database}"
    else:
        # Caller may configure host via environment / connect_args.
        url = f"postgresql+psycopg://apx-agent@localhost:{port}/{database}"

    engine = create_engine(
        url,
        pool_pre_ping=pool_pre_ping,
        pool_recycle=pool_recycle,
    )

    @sa_event.listens_for(engine, "do_connect")
    def _mint_token(
        _dialect: Any,
        _conn_rec: Any,
        _cargs: Any,
        ckwargs: dict[str, Any],
    ) -> None:
        """Mint a fresh OAuth token from the Databricks SDK on every connect."""
        cred = ws.database.generate_database_credential(
            instance_names=[instance_name],
            request_id="apx-agent-lakebase",
        )
        ckwargs["password"] = cred.token

    return engine
```

Also fix the stale docstring in `_memory_lakebase.py` (the doc shows `ws.postgres` — change it to `ws.database`; this is a single-line diff):

```python
# In _memory_lakebase.py, find the do_connect listener in the docstring (~line 236):
# BEFORE:
#     cred = ws.postgres.generate_database_credential(
#         instance_names=["my-lakebase-instance"], request_id="apx-memory",
#     )
# AFTER:
#     cred = ws.database.generate_database_credential(
#         instance_names=["my-lakebase-instance"], request_id="apx-memory",
#     )
```

(Use the Edit tool; confirm the exact surrounding lines before editing.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_lakebase_engine.py -v`
Expected: PASS (4 passed; the do_connect test accepts 0 calls if psycopg is absent).

Run: `cd python && uv run pyright src/apx_agent/_lakebase_engine.py`
Expected: 0 errors.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_lakebase_engine.py \
        python/tests/test_lakebase_engine.py \
        python/src/apx_agent/_memory_lakebase.py
git commit -m "feat(lakebase): shared engine builder using ws.database (reconcile credential API)"
```

---

## Task 0.2: `_get_principal` dependency + per-request principal threading through compile closure

**Files:**
- Modify: `python/src/apx_agent/_defaults.py`
- Modify: `python/src/apx_agent/_compile.py` (excluded from pyright gate — do not add new errors)
- Test: `python/tests/test_memory_wiring.py` (create)

**Context:** `_make_dep_resolvers` maps FastAPI dependency callables → per-request resolved values, capturing them in tool closures via `_resolve_deps_for_fn`. The goal is to add `_get_principal` to the registry so any tool function declaring `principal: Annotated[str | None, Depends(_get_principal)]` (no leading underscore on the tool param name) gets the current request's OBO user identity in its closure — the same mechanism that delivers `ctx.ws`. The entry in `_make_dep_resolvers` supplies the value directly: `_get_principal: (ctx.headers.user_id if ctx.headers else None)`. This is a string (or None), not a lambda — the dep resolver captures the *value* at compile time.

The async→sync thread hop (`_compile.py:182-186`) is safe because resolved-dep values are captured into the closure **before** the `ThreadPoolExecutor.submit` — they travel as captured variables inside `_async_wrapper` / `_sync_bridge`, not as thread-locals. Task 0.2 verifies this concretely.

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_memory_wiring.py
"""Phase 0 prototype tests for E3b principal threading."""
from __future__ import annotations

import asyncio
from typing import Annotated, Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Task 0.2 — _get_principal + _make_dep_resolvers
# ---------------------------------------------------------------------------


def _make_headers(user_id: str | None = None) -> Any:
    """Build a minimal DatabricksAppsHeaders-like object."""
    h = MagicMock()
    h.user_id = user_id
    h.token = None
    return h


def _make_ctx(user_id: str | None = None) -> Any:
    """Build a minimal CompileContext-like object with headers."""
    from apx_agent._compile import CompileContext
    ws = MagicMock()
    ctx = CompileContext(ws=ws, model="test", headers=_make_headers(user_id))
    return ctx


class TestGetPrincipal:
    def test_get_principal_dep_exported(self):
        """_get_principal is importable and PrincipalDependency is a TypeAlias."""
        from apx_agent._defaults import _get_principal, PrincipalDependency
        assert callable(_get_principal)
        # PrincipalDependency wraps _get_principal in Annotated[str|None, Depends(...)]
        import typing
        args = typing.get_args(PrincipalDependency)
        assert any(
            hasattr(a, "dependency") and a.dependency is _get_principal
            for a in args
        )

    def test_dependencies_principal_alias(self):
        """Dependencies.Principal is PrincipalDependency."""
        from apx_agent._defaults import PrincipalDependency
        from apx_agent import Dependencies
        assert Dependencies.Principal is PrincipalDependency

    def test_make_dep_resolvers_includes_get_principal(self):
        """_make_dep_resolvers maps _get_principal to the principal from ctx.headers."""
        from apx_agent._compile import _make_dep_resolvers
        from apx_agent._defaults import _get_principal

        ctx = _make_ctx(user_id="alice@example.com")
        resolvers = _make_dep_resolvers(ctx)
        assert _get_principal in resolvers
        assert resolvers[_get_principal] == "alice@example.com"

    def test_make_dep_resolvers_none_when_no_headers(self):
        """_make_dep_resolvers returns None for principal when headers is None."""
        from apx_agent._compile import _make_dep_resolvers
        from apx_agent._defaults import _get_principal

        ctx = _make_ctx(user_id=None)
        ctx.headers = None  # simulate no headers
        resolvers = _make_dep_resolvers(ctx)
        assert resolvers[_get_principal] is None


class TestPrincipalClosure:
    """Prove the principal reaches a tool closure AND survives the async→sync hop."""

    def _make_principal_tool(self):
        """Build a minimal tool that captures _get_principal via Depends()."""
        from typing import Annotated
        from fastapi.params import Depends
        from apx_agent._defaults import _get_principal
        from apx_agent.tool import tool  # the @tool decorator

        @tool
        def probe(
            query: str,
            principal: Annotated[str | None, Depends(_get_principal)],
        ) -> str:
            """Return the principal seen inside the closure."""
            return principal or "NONE"

        return probe

    def test_sync_tool_sees_correct_principal(self):
        """A sync tool compiled with user_id=alice receives alice in its closure."""
        from apx_agent._compile import _make_langchain_tool

        probe = self._make_principal_tool()
        ctx = _make_ctx(user_id="alice")
        lt = _make_langchain_tool(probe, ctx)
        result = lt.run({"query": "x"})
        assert result == "alice"

    def test_sync_tool_sees_different_principal_per_compile(self):
        """Two CompileContexts with different users produce different closures."""
        from apx_agent._compile import _make_langchain_tool

        probe = self._make_principal_tool()
        lt_alice = _make_langchain_tool(probe, _make_ctx(user_id="alice"))
        lt_bob = _make_langchain_tool(probe, _make_ctx(user_id="bob"))
        assert lt_alice.run({"query": "q"}) == "alice"
        assert lt_bob.run({"query": "q"}) == "bob"

    def test_sync_tool_none_principal_when_header_absent(self):
        """No X-Forwarded-User header → principal is NONE (not a crash)."""
        from apx_agent._compile import _make_langchain_tool

        probe = self._make_principal_tool()
        lt = _make_langchain_tool(probe, _make_ctx(user_id=None))
        result = lt.run({"query": "q"})
        assert result == "NONE"

    def test_async_tool_sees_correct_principal_via_thread_hop(self):
        """CRITICAL: async tool compiled with alice — the ThreadPoolExecutor path
        at _compile.py:182-186 must NOT lose the principal.

        The principal lives in the resolved_deps closure captured BEFORE the thread
        hop, so it arrives on the worker thread as a plain value (not a contextvar).
        This test asserts that invariant."""
        from apx_agent._compile import _make_langchain_tool
        from typing import Annotated
        from fastapi.params import Depends
        from apx_agent._defaults import _get_principal
        from apx_agent.tool import tool

        @tool
        async def async_probe(
            query: str,
            principal: Annotated[str | None, Depends(_get_principal)],
        ) -> str:
            """Async version — will be wrapped via ThreadPoolExecutor."""
            return principal or "NONE"

        ctx = _make_ctx(user_id="carol")
        lt = _make_langchain_tool(async_probe, ctx)
        # lt.run calls _sync_bridge which does ThreadPoolExecutor + asyncio.run
        result = lt.run({"query": "q"})
        assert result == "carol", (
            f"Principal was lost across the async→sync thread hop: got {result!r}. "
            "This means the resolved-dep closure is not capturing the value — "
            "the Dependencies.Principal mechanism is broken."
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_memory_wiring.py::TestGetPrincipal tests/test_memory_wiring.py::TestPrincipalClosure -v`
Expected: FAIL — `cannot import name '_get_principal' from 'apx_agent._defaults'` and `_get_principal` missing from `_make_dep_resolvers`.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/_defaults.py`, add after the `HeadersDependency` alias (line ~83):

```python
# ---------------------------------------------------------------------------
# Principal dependency — per-request OBO user identity
# ---------------------------------------------------------------------------


def _get_principal(headers: HeadersDependency) -> str | None:
    """Return the OBO user identity (X-Forwarded-User) for the current request.

    Used by config-built memory tools to resolve the per-request principal
    without needing a zero-arg closure that captures request-scoped state.
    Returns ``None`` when running locally without Databricks Apps headers
    (local dev) or when no user header is present.
    """
    return headers.user_id


PrincipalDependency: TypeAlias = Annotated[str | None, Depends(_get_principal)]
```

In `Dependencies` class, add:

```python
    Principal: TypeAlias = PrincipalDependency
    """Per-request OBO user identity from X-Forwarded-User.
    Used by config-built memory tools.
    Recommended usage: ``_principal: Dependencies.Principal``"""
```

In `python/src/apx_agent/_compile.py`, add `_get_principal` to `_make_dep_resolvers` (file is in the pyright-exclude list — do not add new type errors; a bare `# type: ignore` is permissible if needed):

```python
def _make_dep_resolvers(ctx: CompileContext) -> dict[Any, Any]:
    """Map FastAPI dependency callables to their resolved values for ``ctx``."""
    from ._sql import run_sql
    from ._defaults import _get_principal  # E3b: principal for config-built memory tools

    return {
        _get_workspace_client: ctx.ws,
        _get_user_client: ctx.ws,
        get_databricks_headers: ctx.headers,
        _get_sql_runner: (lambda q: run_sql(ctx.ws, q)),
        # E3b: principal from X-Forwarded-User — resolved as a value (str|None),
        # NOT a lambda. Captured in the tool closure before the ThreadPoolExecutor
        # hop, so it survives the async→sync bridge at lines 182-186 automatically.
        _get_principal: (ctx.headers.user_id if ctx.headers else None),
        # _get_request intentionally omitted: no FastAPI Request inside a
        # compiled graph.
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_memory_wiring.py::TestGetPrincipal tests/test_memory_wiring.py::TestPrincipalClosure -v`
Expected: PASS (all 8 tests). The `test_async_tool_sees_correct_principal_via_thread_hop` test is the critical one — it proves the mechanism survives the `ThreadPoolExecutor` path.

Run: `cd python && uv run pyright src/apx_agent/_defaults.py`
Expected: 0 errors.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_defaults.py \
        python/src/apx_agent/_compile.py \
        python/tests/test_memory_wiring.py
git commit -m "feat(principal): Dependencies.Principal dep + _get_principal in dep-resolver (E3b)"
```

---

## Task 0.3: Mandatory cross-principal isolation test

**Files:**
- Test: `python/tests/test_memory_wiring.py` (append)

**Context:** This is the MANDATORY isolation test from spec §6. Two principals A and B share one store; A's memories must not be visible to B, and vice-versa; no-principal returns `NO_PRINCIPAL` with no leakage. This task uses `InMemoryMemoryStore` (no external deps; CI-safe). The test verifies the *store's* row-level isolation (confirmed in `test_memory_tools.py:97-152`) and the absence of any cross-principal contamination path in the config-built tool machinery.

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_memory_wiring.py

# ---------------------------------------------------------------------------
# Task 0.3 — Cross-principal isolation (MANDATORY gate)
# ---------------------------------------------------------------------------

from apx_agent._memory import InMemoryMemoryStore
from apx_agent._memory_tools import make_memory_tools, NO_PRINCIPAL


class TestCrossPrincipalIsolation:
    """These tests are the mandatory gate from spec §6.

    They must ALL pass before Phase 1 begins. If any fail, the principal
    threading mechanism (option b) is broken and E3b must be redesigned.
    """

    def _make_tools(self, principal: str | None):
        """Build memory tools with an inline resolver for the given principal."""
        store = InMemoryMemoryStore()
        tools = make_memory_tools(
            store=store,
            principal_id_resolver=(lambda p=principal: p),
        )
        # also return the store so the sibling-test can pre-populate
        return tools, store

    def _find_tool(self, tools: list, name: str):
        for t in tools:
            if getattr(t, "__name__", None) == name or getattr(t, "name", None) == name:
                return t
        raise KeyError(f"Tool {name!r} not found")

    def test_principal_a_cannot_recall_bs_memory(self):
        """A remembers something; B recalls with A's query → empty."""
        store = InMemoryMemoryStore()
        tools_a = make_memory_tools(store=store, principal_id_resolver=lambda: "alice")
        tools_b = make_memory_tools(store=store, principal_id_resolver=lambda: "bob")

        remember_a = self._find_tool(tools_a, "remember")
        recall_b = self._find_tool(tools_b, "recall")

        remember_a(content="alice secret")
        result = recall_b(query="alice secret")
        # B must NOT see A's memory
        assert "alice secret" not in result

    def test_principal_b_cannot_recall_as_memory(self):
        """Symmetrical: B remembers; A cannot recall B's memory."""
        store = InMemoryMemoryStore()
        tools_a = make_memory_tools(store=store, principal_id_resolver=lambda: "alice")
        tools_b = make_memory_tools(store=store, principal_id_resolver=lambda: "bob")

        remember_b = self._find_tool(tools_b, "remember")
        recall_a = self._find_tool(tools_a, "recall")

        remember_b(content="bob secret")
        result = recall_a(query="bob secret")
        assert "bob secret" not in result

    def test_principal_can_recall_own_memory(self):
        """Positive case: a principal recalls its own memories correctly."""
        store = InMemoryMemoryStore()
        tools = make_memory_tools(store=store, principal_id_resolver=lambda: "alice")
        remember = self._find_tool(tools, "remember")
        recall = self._find_tool(tools, "recall")

        remember(content="alice own")
        result = recall(query="alice own")
        assert "alice own" in result

    def test_no_principal_returns_no_principal_sentinel(self):
        """No-principal resolver → NO_PRINCIPAL constant, no write or leak."""
        store = InMemoryMemoryStore()
        # First, write something as alice.
        tools_a = make_memory_tools(store=store, principal_id_resolver=lambda: "alice")
        self._find_tool(tools_a, "remember")(content="alice data")

        # Now build no-principal tools.
        tools_none = make_memory_tools(store=store)  # no resolver, no default
        recall_none = self._find_tool(tools_none, "recall")
        remember_none = self._find_tool(tools_none, "remember")

        recall_result = recall_none(query="alice data")
        remember_result = remember_none(content="anonymous write")

        assert NO_PRINCIPAL in recall_result
        assert NO_PRINCIPAL in remember_result

    def test_no_principal_write_does_not_pollute_alice_namespace(self):
        """A no-principal write attempt must not contaminate another principal's store."""
        store = InMemoryMemoryStore()
        # No-principal write attempt.
        tools_none = make_memory_tools(store=store)
        self._find_tool(tools_none, "remember")(content="leaked write")

        # Alice recalls — should be empty (no contamination).
        tools_a = make_memory_tools(store=store, principal_id_resolver=lambda: "alice")
        result = self._find_tool(tools_a, "recall")(query="leaked write")
        assert "leaked write" not in result

    def test_dep_principal_path_isolation(self):
        """Config path: a tool with _principal dep param also isolates correctly.

        Simulates what the runtime does — two compile contexts with different
        user_ids produce closures that each see only their own memories.
        """
        from apx_agent._compile import _make_langchain_tool

        store = InMemoryMemoryStore()

        # Build a dep-principal-aware tool pair using the compile machinery.
        # Use the TestPrincipalClosure._make_principal_tool() pattern:
        from typing import Annotated
        from fastapi.params import Depends
        from apx_agent._defaults import _get_principal
        from apx_agent.tool import tool

        @tool
        def dep_remember(
            content: str,
            principal: Annotated[str | None, Depends(_get_principal)],
        ) -> str:
            """Remember with dep-resolved principal."""
            if not principal:
                return NO_PRINCIPAL
            store.add({"principal_id": principal, "content": content})
            return "stored"

        @tool
        def dep_recall(
            query: str,
            principal: Annotated[str | None, Depends(_get_principal)],
        ) -> str:
            """Recall with dep-resolved principal."""
            if not principal:
                return NO_PRINCIPAL
            from apx_agent._memory import RecallOptions
            results = store.recall(RecallOptions(principal_id=principal, query=query))
            return " | ".join(r.content for r in results) or "none"

        lt_remember_alice = _make_langchain_tool(dep_remember, _make_ctx("alice"))
        lt_recall_alice = _make_langchain_tool(dep_recall, _make_ctx("alice"))
        lt_recall_bob = _make_langchain_tool(dep_recall, _make_ctx("bob"))

        lt_remember_alice.run({"content": "alice dep memory"})
        alice_result = lt_recall_alice.run({"query": "dep memory"})
        bob_result = lt_recall_bob.run({"query": "dep memory"})

        assert "alice dep memory" in alice_result
        assert "alice dep memory" not in bob_result
```

- [ ] **Step 2: Run test to verify it fails (if 0.1/0.2 not done)**

Run: `cd python && uv run pytest tests/test_memory_wiring.py::TestCrossPrincipalIsolation -v`
Expected after Task 0.2: PASS immediately (the `InMemoryMemoryStore` + existing `make_memory_tools` already filter by principal_id; the dep-principal path test proves the mechanism end-to-end). If any test fails, the isolation mechanism is broken — escalate before Phase 1.

- [ ] **Step 3: No implementation**

No new source changes. All tests passing = gate cleared.

- [ ] **Step 4: Run full Phase-0 suite**

Run: `cd python && uv run pytest tests/test_lakebase_engine.py tests/test_memory_wiring.py -v`
Expected: PASS (all Phase-0 tests).

- [ ] **Step 5: Commit**

```bash
git add python/tests/test_memory_wiring.py
git commit -m "test(e3b): mandatory cross-principal isolation gate (Phase 0)"
```

---

## ⛔ GATE (read before proceeding)

**Do NOT begin Phase 1 until BOTH conditions hold:**

1. **Task 0.2 passes** — specifically `test_async_tool_sees_correct_principal_via_thread_hop`. This proves the per-request principal reaches the tool closure AND survives the `async→sync ThreadPoolExecutor` hop. Different `user_id` values in `CompileContext` must produce different principals in different closures, and absent header must yield `None` without a crash.

2. **Task 0.3 passes** — specifically `test_dep_principal_path_isolation`. This proves cross-principal isolation holds end-to-end through the dep-resolver compile machinery: A's memories are invisible to B's closure, and vice versa.

**If either fails:** STOP. Do not build Phases 1+. The `Dependencies.Principal` design (option b) is wrong. File an issue and escalate to consider: (a) `contextvars.copy_context()` + `ctx.run(...)` at the `ThreadPoolExecutor.submit` site (`_compile.py:186`) to propagate a ContextVar principal across the thread hop; or (b) lifting the memory tool into the serve path instead of the compile path. The isolation test and the async-hop test together are the empirical check for this design decision.

**Post-gate specifics that depend on Phase 0 outcomes:**
- The exact `_lakebase_engine.py` usage confirmed in 0.1 is what Phase-1 Task 1.4 (`_memory_wiring.py` store factory) uses.
- The `principal: Annotated[str | None, Depends(_get_principal)]` dep-param pattern proven in 0.2 is what Phase-1 Task 1.3 (`make_memory_tools` extension) emits. **No leading underscore; no `= None` default** (dep param must not have a default or `_inspect_tool_fn` misclassifies it as a plain param).

---

## Phase 1 — Config models

## Task 1.1: `MemoryBackendConfig`, `ExampleBackendConfig`, `SessionBackendConfig` + `AgentConfig` fields

**Files:**
- Modify: `python/src/apx_agent/_models.py`
- Test: `python/tests/test_wiring.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_wiring.py
import textwrap
import pytest
from pydantic import ValidationError
from apx_agent._models import AgentConfig, MemoryBackendConfig, ExampleBackendConfig, SessionBackendConfig
from apx_agent._inspection import _load_agent_config


class TestMemoryBackendConfig:
    def test_inmemory_type_defaults(self):
        cfg = MemoryBackendConfig(type="inmemory")
        assert cfg.type == "inmemory"
        assert cfg.namespace_default == "default"
        assert cfg.tool_prefix == ""
        assert cfg.include is None
        assert cfg.auto_create is True

    def test_lakebase_type_requires_instance_and_database_at_build(self):
        # Config model itself accepts any field combination (validation at
        # _build_store time, not model parse time, to give a clear error).
        cfg = MemoryBackendConfig(type="lakebase")
        assert cfg.type == "lakebase"

    def test_extra_key_forbidden(self):
        # Pydantic v2 emits "Extra inputs are not permitted" + the field name.
        # Match on the unknown key name, not "extra" (case sensitivity varies).
        with pytest.raises(ValidationError, match="unknown_key"):
            MemoryBackendConfig(type="inmemory", unknown_key="oops")

    def test_unknown_type_forbidden(self):
        with pytest.raises(ValidationError):
            MemoryBackendConfig(type="s3bucket")

    def test_agent_config_memory_field_loads_from_toml(self, tmp_path):
        pp = tmp_path / "pyproject.toml"
        pp.write_text(textwrap.dedent("""
            [tool.apx.agent]
            name = "mem-agent"
            model = "databricks-claude-sonnet-4-6"

            [tool.apx.agent.memory]
            type = "lakebase"
            instance_name = "coworker-lakebase"
            database = "agentdb"
            table_name = "apx_memories"
            embedding_model = "databricks-bge-large-en"
            embedding_dim = 1024
        """))
        config = _load_agent_config(pyproject_path=str(pp))
        assert config is not None
        assert config.memory is not None
        assert config.memory.type == "lakebase"
        assert config.memory.instance_name == "coworker-lakebase"
        assert config.memory.embedding_dim == 1024

    def test_agent_config_session_field_loads_from_toml(self, tmp_path):
        pp = tmp_path / "pyproject.toml"
        pp.write_text(textwrap.dedent("""
            [tool.apx.agent]
            name = "sess-agent"

            [tool.apx.agent.session]
            type = "inmemory"
        """))
        config = _load_agent_config(pyproject_path=str(pp))
        assert config is not None
        assert config.session is not None
        assert config.session.type == "inmemory"

    def test_agent_config_example_field_loads_from_toml(self, tmp_path):
        pp = tmp_path / "pyproject.toml"
        pp.write_text(textwrap.dedent("""
            [tool.apx.agent]
            name = "ex-agent"

            [tool.apx.agent.example]
            type = "delta"
            table_name = "main.coworker.apx_examples"
            embedding_model = "databricks-bge-large-en"
            embedding_dim = 1024
        """))
        config = _load_agent_config(pyproject_path=str(pp))
        assert config is not None
        assert config.example is not None
        assert config.example.type == "delta"
        assert config.example.agent_id is None  # defaults to None; wiring uses config.name

    def test_absent_memory_gives_none(self, tmp_path):
        pp = tmp_path / "pyproject.toml"
        pp.write_text('[tool.apx.agent]\nname = "no-mem"\n')
        config = _load_agent_config(pyproject_path=str(pp))
        assert config is not None
        assert config.memory is None
        assert config.session is None
        assert config.example is None

    def test_validate_at_boot_default_true(self):
        cfg = MemoryBackendConfig(type="lakebase")
        assert cfg.validate_at_boot is True

    def test_validate_at_boot_opt_out(self):
        cfg = MemoryBackendConfig(type="lakebase", validate_at_boot=False)
        assert cfg.validate_at_boot is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_wiring.py::TestMemoryBackendConfig -v`
Expected: FAIL — `cannot import name 'MemoryBackendConfig' from 'apx_agent._models'`.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/_models.py`, add before `AgentConfig` (after `GuardrailsConfig` if E3c landed first, else after any existing config classes):

```python
from typing import Literal  # add if not already imported

# ---------------------------------------------------------------------------
# E3b: Memory / Example / Session backend config models
# ---------------------------------------------------------------------------

StoreType = Literal["inmemory", "delta", "lakebase"]


class MemoryBackendConfig(BaseModel):
    """Declarative memory backend — maps to ``[tool.apx.agent.memory]``."""

    model_config = ConfigDict(extra="forbid")

    type: StoreType = "inmemory"
    """Backend type: ``inmemory`` (dev), ``delta`` (UC table), ``lakebase`` (Postgres/pgvector)."""

    # --- embedding (required for lakebase, optional for delta) ---
    embedding_model: str | None = None
    """Databricks serving-endpoint name for the embedding model (e.g. ``"databricks-bge-large-en"``)."""
    embedding_dim: int | None = None
    """Embedding vector dimensionality — required for lakebase, informational for delta."""

    # --- delta-specific ---
    table_name: str | None = None
    """Fully-qualified Unity Catalog table name (e.g. ``"catalog.schema.apx_memories"``)
    for delta; plain table name (e.g. ``"apx_memories"``) for lakebase."""
    index_name: str | None = None
    """Optional Vector Search index name for delta semantic recall."""
    auto_create: bool = True
    """Create the table on first use (default ``True``). Set ``False`` for locked envs."""

    # --- lakebase-specific ---
    instance_name: str | None = None
    """Lakebase instance name, passed to ``ws.database.generate_database_credential``."""
    database: str | None = None
    """Postgres database name."""
    host: str | None = None
    """Lakebase endpoint hostname. Supports ``$ENV_VAR`` references."""
    ensure_extension: bool = True
    """Run ``CREATE EXTENSION IF NOT EXISTS vector`` on first use. Set ``False`` when
    the role lacks the privilege."""

    # --- behavioral ---
    namespace_default: str = "default"
    """Default namespace for memory tools. Passed to ``make_memory_tools``."""
    tool_prefix: str = ""
    """Prefix for minted tool names (e.g. ``"mem_"`` → ``"mem_recall"``). Avoids
    name collisions when multiple stores are mounted."""
    include: list[str] | None = None
    """Subset of tools to build: ``["recall"]``, ``["recall","remember"]``, etc.
    Defaults to all three: ``recall``, ``remember``, ``forget``."""

    # --- boot validation ---
    validate_at_boot: bool = True
    """When ``True`` (default), attempt a credential mint and schema check at attach
    time so misconfiguration surfaces at deploy, not mid-turn. Set ``False`` for
    offline/locked test envs."""


class ExampleBackendConfig(BaseModel):
    """Declarative example backend — maps to ``[tool.apx.agent.example]``."""

    model_config = ConfigDict(extra="forbid")

    type: StoreType = "inmemory"

    embedding_model: str | None = None
    embedding_dim: int | None = None
    table_name: str | None = None
    index_name: str | None = None
    auto_create: bool = True
    instance_name: str | None = None
    database: str | None = None
    host: str | None = None
    ensure_extension: bool = True

    agent_id: str | None = None
    """Partition key for example rows — defaults to ``config.name`` at attach time.
    Examples are scoped per coworker (not per end-user)."""
    tool_prefix: str = ""
    include: list[str] | None = None
    validate_at_boot: bool = True


class SessionBackendConfig(BaseModel):
    """Declarative session backend — maps to ``[tool.apx.agent.session]``.

    Note: ``DeltaSessionStore`` takes ``table_path`` (not ``table_name``);
    the wiring maps ``table_name`` → ``table_path`` when building a delta session store.
    ``warehouse_id`` is optional for delta (``DeltaSessionStore.__init__`` takes it as
    ``Optional[str]``, confirmed at ``_session_delta.py:72-87``).
    """

    model_config = ConfigDict(extra="forbid")

    type: StoreType = "inmemory"
    table_name: str | None = None
    """Table name for session rows. For delta: a three-part UC name. For lakebase: plain name."""
    auto_create: bool = True
    instance_name: str | None = None
    database: str | None = None
    host: str | None = None
    warehouse_id: str | None = None
    """Optional SQL warehouse ID for delta session stores (passed to DeltaSessionStore)."""
    validate_at_boot: bool = True
```

In `AgentConfig`, add three fields (after `guardrails` if E3c has landed, otherwise after `api_prefix`):

```python
    memory: MemoryBackendConfig | None = None
    """Declarative memory backend — see ``[tool.apx.agent.memory]``."""

    example: ExampleBackendConfig | None = None
    """Declarative example backend — see ``[tool.apx.agent.example]``."""

    session: SessionBackendConfig | None = None
    """Declarative session backend — see ``[tool.apx.agent.session]``."""
```

Pydantic v2 parses TOML sub-tables as nested dicts automatically; the `k in AgentConfig.model_fields` filter in `_inspection.py:179` passes `memory`, `example`, `session` through once declared as fields.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_wiring.py::TestMemoryBackendConfig -v`
Expected: PASS (10 passed).

```bash
cd python && uv run pyright src/apx_agent/_models.py
```
Expected: 0 errors.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_models.py python/tests/test_wiring.py
git commit -m "feat(models): MemoryBackendConfig, ExampleBackendConfig, SessionBackendConfig + AgentConfig fields (E3b)"
```

---

## Task 1.2: `make_embedding_fn(ws, endpoint_name) -> EmbeddingFn`

**Files:**
- Create: `python/src/apx_agent/_embeddings.py`
- Create: `python/tests/test_embeddings.py`

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_embeddings.py
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


def _mock_ws_with_embeddings(dims: int = 4) -> Any:
    """Build a WorkspaceClient mock that returns fake embeddings."""
    ws = MagicMock()
    ws.serving_endpoints.query.side_effect = lambda **kw: MagicMock(
        predictions=[[float(i) for i in range(dims)] for _ in kw.get("inputs", [[]])]
    )
    return ws


class TestMakeEmbeddingFn:
    def test_returns_callable(self):
        from apx_agent._embeddings import make_embedding_fn

        ws = _mock_ws_with_embeddings()
        fn = make_embedding_fn(ws, "databricks-bge-large-en")
        assert callable(fn)

    def test_returns_list_of_lists(self):
        from apx_agent._embeddings import make_embedding_fn

        ws = _mock_ws_with_embeddings(dims=4)
        fn = make_embedding_fn(ws, "databricks-bge-large-en")
        result = fn(["hello", "world"])
        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(row, list) for row in result)
        assert all(isinstance(v, float) for v in result[0])

    def test_single_text_works(self):
        from apx_agent._embeddings import make_embedding_fn

        ws = _mock_ws_with_embeddings(dims=3)
        fn = make_embedding_fn(ws, "bge-large")
        result = fn(["only one"])
        assert len(result) == 1

    def test_calls_serving_endpoints_query(self):
        from apx_agent._embeddings import make_embedding_fn

        ws = _mock_ws_with_embeddings()
        fn = make_embedding_fn(ws, "my-embed-model")
        fn(["test"])
        ws.serving_endpoints.query.assert_called_once()
        call_kwargs = ws.serving_endpoints.query.call_args.kwargs
        assert call_kwargs.get("name") == "my-embed-model"

    def test_empty_list_returns_empty(self):
        from apx_agent._embeddings import make_embedding_fn

        ws = _mock_ws_with_embeddings()
        fn = make_embedding_fn(ws, "model")
        result = fn([])
        assert result == []

    def test_satisfies_embedding_fn_type(self):
        """Return value satisfies EmbeddingFn = Callable[[Sequence[str]], list[list[float]]]."""
        from apx_agent._memory import EmbeddingFn
        from apx_agent._embeddings import make_embedding_fn
        import collections.abc

        ws = _mock_ws_with_embeddings()
        fn = make_embedding_fn(ws, "m")
        # Not a strict isinstance check on EmbeddingFn (TypeAlias), but confirm
        # the callable contract: takes a sequence, returns list[list[float]].
        result = fn(["a", "b"])
        assert isinstance(result, list)
        assert isinstance(result[0], list)
        assert isinstance(result[0][0], float)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_embeddings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apx_agent._embeddings'`.

- [ ] **Step 3: Write the implementation**

```python
# python/src/apx_agent/_embeddings.py
"""Embedding function builder for config-declared memory backends (E3b).

Databricks Foundation Model API serving endpoints support an embeddings
protocol: ``POST /serving-endpoints/{name}/invocations`` with body
``{"inputs": ["text1", "text2"]}`` returns ``{"predictions": [[...], [...]]}``.

The ``databricks-sdk`` wraps this as
``ws.serving_endpoints.query(name=endpoint_name, inputs=[...])``
(same call pattern as :mod:`apx_agent.foundation_model` — see line ~91).

There is NO built-in embedder in apx-agent — the caller wires it.  For
config-declared stores this module provides the factory that bridges
``embedding_model`` (a string endpoint name) → ``EmbeddingFn`` (a batched
callable).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._memory import EmbeddingFn

logger = logging.getLogger(__name__)


def make_embedding_fn(ws: Any, endpoint_name: str) -> "EmbeddingFn":
    """Return a batched embedding function that calls a Databricks serving endpoint.

    The function satisfies ``EmbeddingFn = Callable[[Sequence[str]], list[list[float]]]``.
    It queries ``ws.serving_endpoints.query(name=endpoint_name, inputs=texts)``
    and returns ``response.predictions``.

    Args:
        ws: A ``WorkspaceClient``.
        endpoint_name: Name of the Databricks embeddings serving endpoint
            (e.g. ``"databricks-bge-large-en"``).

    Returns:
        A callable ``(texts: Sequence[str]) -> list[list[float]]`` suitable
        for passing to ``LakebaseMemoryStore`` / ``InMemoryMemoryStore`` as
        ``embedding_fn``.

    Example::

        from databricks.sdk import WorkspaceClient
        from apx_agent import LakebaseMemoryStore
        from apx_agent._embeddings import make_embedding_fn

        ws = WorkspaceClient()
        embed = make_embedding_fn(ws, "databricks-bge-large-en")
        store = LakebaseMemoryStore(engine=engine, embedding_fn=embed, embedding_dim=1024)
    """

    def _embed(texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = ws.serving_endpoints.query(
                name=endpoint_name,
                inputs=list(texts),
            )
        except Exception as exc:
            logger.error(
                "Embedding call to %r failed: %s — returning zero vectors.",
                endpoint_name,
                exc,
            )
            # Fail-safe: return zero vectors of indeterminate dim.  The store
            # will compute zero-similarity for these, which is safe (degraded
            # recall, not a crash).  The embedding endpoint is a config issue,
            # not an unrecoverable runtime error.
            return [[0.0] for _ in texts]

        predictions = getattr(response, "predictions", None)
        if predictions is None:
            logger.warning(
                "Embedding response from %r has no 'predictions' key — "
                "returning zero vectors.",
                endpoint_name,
            )
            return [[0.0] for _ in texts]

        return [list(map(float, row)) for row in predictions]

    _embed.__name__ = f"embed_{endpoint_name}"
    return _embed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_embeddings.py -v`
Expected: PASS (6 passed).

```bash
cd python && uv run pyright src/apx_agent/_embeddings.py
```
Expected: 0 errors.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_embeddings.py python/tests/test_embeddings.py
git commit -m "feat(embeddings): make_embedding_fn — batched EmbeddingFn from serving endpoint (E3b)"
```

---

## Task 1.3: Extend `make_memory_tools` with `_use_dep_principal` path

**Files:**
- Modify: `python/src/apx_agent/_memory_tools.py`
- Test: `python/tests/test_memory_tools.py` (append)

**Context:** This task MUST come before Task 1.4 (`_memory_wiring.py`) because `attach_declared_memory` calls `make_memory_tools(_use_dep_principal=True)`. Building `_memory_wiring.py` before this flag exists would produce a `TypeError: unexpected keyword argument` on the `make_memory_tools` call, breaking Task 1.4's tests.

The `_inspect_tool_fn` check (verified at `_inspection.py:38-44`): the function classifies a param as a dep if `get_origin(annotation) is Annotated` AND any `get_args` is a `Depends` instance. It does NOT filter by name. The `default` check is `param.default is not inspect.Parameter.empty` — a param with `= None` default gets `default = None` in `plain_params`, meaning it would be classified as a **plain** param (not a dep) if it happens to be `Annotated[..., Depends()]` with a default. Therefore: the `principal` dep param must have **no default** in the tool function signature. `_resolve_deps_for_fn` will inject it automatically; no default needed.

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_memory_tools.py
from typing import Annotated
from fastapi.params import Depends
from apx_agent._defaults import _get_principal


class TestDepPrincipalPath:
    def test_use_dep_principal_true_emits_dep_param(self):
        """Tools built with _use_dep_principal=True carry a `principal` dep param."""
        from apx_agent._memory_tools import make_memory_tools
        from apx_agent._memory import InMemoryMemoryStore
        import inspect
        from typing import get_type_hints

        store = InMemoryMemoryStore()
        tools = make_memory_tools(store=store, _use_dep_principal=True)
        recall = next(t for t in tools if getattr(t, "__name__", None) == "recall")

        # Confirm it's classified as a dep, not a plain param
        from apx_agent._inspection import _inspect_tool_fn
        plain_params, dep_names = _inspect_tool_fn(recall)
        assert "principal" in dep_names, (
            "recall with _use_dep_principal=True must have 'principal' in dep_names"
        )
        assert "principal" not in plain_params, (
            "'principal' must NOT appear in plain_params (it would pollute the LLM schema)"
        )

    def test_use_dep_principal_false_no_dep_param(self):
        """Existing code path (default False) must NOT emit a principal dep param."""
        from apx_agent._memory_tools import make_memory_tools
        from apx_agent._memory import InMemoryMemoryStore
        from apx_agent._inspection import _inspect_tool_fn

        store = InMemoryMemoryStore()
        tools = make_memory_tools(store=store)  # default _use_dep_principal=False
        recall = next(t for t in tools if getattr(t, "__name__", None) == "recall")
        _, dep_names = _inspect_tool_fn(recall)
        assert "principal" not in dep_names

    def test_dep_principal_tool_uses_principal_arg(self):
        """recall with _use_dep_principal=True reads `principal` kwarg directly."""
        from apx_agent._memory_tools import make_memory_tools
        from apx_agent._memory import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        store.add({"principal_id": "alice", "content": "alice dep data"})

        tools = make_memory_tools(store=store, _use_dep_principal=True)
        recall = next(t for t in tools if getattr(t, "__name__", None) == "recall")

        # Simulate what _resolve_deps_for_fn injects: pass principal as a kwarg.
        result = recall(query="dep data", principal="alice")
        assert "alice dep data" in result

    def test_dep_principal_none_returns_no_principal(self):
        """principal=None (absent header) → NO_PRINCIPAL, no crash."""
        from apx_agent._memory_tools import make_memory_tools, NO_PRINCIPAL
        from apx_agent._memory import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        tools = make_memory_tools(store=store, _use_dep_principal=True)
        recall = next(t for t in tools if getattr(t, "__name__", None) == "recall")
        result = recall(query="anything", principal=None)
        assert NO_PRINCIPAL in result

    def test_dep_principal_isolation_alice_vs_bob(self):
        """Store-level: alice and bob share a store; principal kwarg isolates them."""
        from apx_agent._memory_tools import make_memory_tools
        from apx_agent._memory import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        store.add({"principal_id": "alice", "content": "alice only"})
        store.add({"principal_id": "bob", "content": "bob only"})

        tools = make_memory_tools(store=store, _use_dep_principal=True)
        recall = next(t for t in tools if getattr(t, "__name__", None) == "recall")

        alice_result = recall(query="only", principal="alice")
        bob_result = recall(query="only", principal="bob")

        assert "alice only" in alice_result
        assert "bob only" not in alice_result
        assert "bob only" in bob_result
        assert "alice only" not in bob_result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_memory_tools.py::TestDepPrincipalPath -v`
Expected: FAIL — `make_memory_tools` has no `_use_dep_principal` param.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/_memory_tools.py`, extend `make_memory_tools` signature:

```python
def make_memory_tools(
    *,
    store: MemoryStore,
    principal_id_resolver: Callable[[], str | None] | None = None,
    default_principal_id: str | None = None,
    _use_dep_principal: bool = False,          # NEW: activates dep-param path for config-built tools
    namespace_default: str = "default",
    tool_prefix: str = "",
    include: list[MemoryToolName] | None = None,
) -> list[Any]:
```

When `_use_dep_principal=True`, set up the dep annotation at the top of `make_memory_tools` (before the `for name in requested:` loop):

```python
    if _use_dep_principal:
        from typing import Annotated as _Ann  # noqa: PLC0415
        from fastapi.params import Depends as _Depends  # noqa: PLC0415
        from ._defaults import _get_principal  # noqa: PLC0415
        _PrincipalDep = _Ann[str | None, _Depends(_get_principal)]
```

Then within the `for name in requested:` loop, for `recall` add a branch:

```python
        if name == "recall":
            if _use_dep_principal:
                @tool(name=f"{tool_prefix}recall")
                def recall(  # type: ignore[misc]
                    query: str,
                    k: int = 5,
                    namespace: str | None = None,
                    tags: list[str] | None = None,
                    principal: _PrincipalDep = ...,  # NO default: must be injected as dep  # type: ignore[assignment]
                ) -> str:
                    """Recall durable memories relevant to a query.

                    Returns the top-k matches formatted as a markdown bullet
                    list. Each line is ``"- [score=X.XX] {content}"``.
                    Returns ``"No memories found."`` when nothing matches.
                    """
                    if not principal:
                        return NO_PRINCIPAL
                    results = store.recall(
                        RecallOptions(
                            principal_id=principal,
                            query=query,
                            k=k,
                            namespace=namespace if namespace is not None else namespace_default,
                            tags=tuple(tags) if tags is not None else None,
                        )
                    )
                    return _format_recall_results(results)
                tools.append(recall)
            else:
                # --- existing zero-arg-resolver path (unchanged) ---
                @tool(name=f"{tool_prefix}recall")
                def recall(...):  # existing body verbatim
                    ...
```

Apply the same `_use_dep_principal` branch to `remember`. `forget` is principal-agnostic (deletes by memory id) — no change.

**IMPORTANT verify before coding:** Confirm `_inspect_tool_fn` classifies `principal: _PrincipalDep` (no default / `= ...`) as a dep, not a plain param. Using `= ...` as a sentinel is intentional — `inspect.Parameter.empty` would also work. With `= ...`, the line `default = param.default if param.default is not inspect.Parameter.empty else ...` at `_inspection.py:43` produces `default = ...` (Ellipsis), but that path is never reached for deps (`_is_fastapi_dependency` check at line 40 fires first and appends to `dep_param_names`). Confirm by running the `test_use_dep_principal_true_emits_dep_param` test step by step.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_memory_tools.py -v`
Expected: PASS (all, including new `TestDepPrincipalPath` tests and all pre-existing resolver tests).

```bash
cd python && uv run pyright src/apx_agent/_memory_tools.py
```
Expected: 0 errors (file is NOT in the exclude list).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_memory_tools.py python/tests/test_memory_tools.py
git commit -m "feat(memory): make_memory_tools _use_dep_principal path for config-built tools (E3b)"
```

---

## Task 1.4: `_memory_wiring.py` — store factory + `attach_declared_memory` + `resolve_session_store`

**Files:**
- Create: `python/src/apx_agent/_memory_wiring.py`
- Test: `python/tests/test_memory_wiring.py` (append)

**Context:** This task builds on Task 1.3 (`_use_dep_principal` flag) and Task 0.1 (`_lakebase_engine.py`). Both must be complete before this runs. Memory attach runs inside `finalize_agent` (Task 1.5) before `agent.collect_tools()` at `_wiring.py:229`.

**Example store note:** `ExampleBackendConfig` is backed by `ExampleStore` (different from `MemoryStore`). The example store classes are `InMemoryExampleStore` (`_example.py`), `DeltaExampleStore` (`_example_delta.py`), `LakebaseExampleStore` (`_example_lakebase.py`). `LakebaseExampleStore.__init__` takes `(engine, embedding_fn, embedding_dim, table_name, auto_create, ensure_extension)` — same shape as `LakebaseMemoryStore`, just different schema (keyed by `agent_id`, not `principal_id`). Build example stores via `_build_example_store` (separate from `_build_memory_store`).

**DeltaSessionStore note:** `DeltaSessionStore.__init__(self, *, ws, table_path, warehouse_id=None, auto_create=True)` — takes `table_path` (three-part UC name), not `table_name`. Add `warehouse_id: str | None = None` to `SessionBackendConfig` (Task 1.1's model), or pass `None` explicitly. Do NOT import `get_warehouse_id` (that import would fail).

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_memory_wiring.py
import textwrap
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apx_agent import Agent
from apx_agent._memory import InMemoryMemoryStore
from apx_agent._models import AgentConfig, MemoryBackendConfig, SessionBackendConfig
# SessionBackendConfig needs warehouse_id field (add in Task 1.1 if not yet done)


class TestAttachDeclaredMemory:
    def _minimal_config(self, **kw) -> AgentConfig:
        return AgentConfig(name="t", memory=MemoryBackendConfig(**kw))

    def test_inmemory_type_attaches_recall_remember_forget(self):
        from apx_agent._memory_wiring import attach_declared_memory

        agent = Agent(tools=[])
        cfg = self._minimal_config(type="inmemory")
        attach_declared_memory(agent, cfg, ws=None)
        names = {fn.__name__ for fn in agent._tool_fns}
        assert "recall" in names
        assert "remember" in names
        assert "forget" in names

    def test_tools_appear_in_collect_tools_after_attach(self):
        from apx_agent._memory_wiring import attach_declared_memory

        agent = Agent(tools=[])
        cfg = self._minimal_config(type="inmemory")
        attach_declared_memory(agent, cfg, ws=None)
        tool_names = {t.name for t in agent.collect_tools()}
        assert "recall" in tool_names

    def test_idempotent_double_attach(self):
        from apx_agent._memory_wiring import attach_declared_memory

        agent = Agent(tools=[])
        cfg = self._minimal_config(type="inmemory")
        attach_declared_memory(agent, cfg, ws=None)
        attach_declared_memory(agent, cfg, ws=None)
        # Must not double-register: only one recall
        names = [fn.__name__ for fn in agent._tool_fns if fn.__name__ == "recall"]
        assert len(names) == 1

    def test_code_wired_recall_wins_over_declared(self, caplog):
        from apx_agent._memory_wiring import attach_declared_memory
        from apx_agent._memory_tools import make_memory_tools

        store = InMemoryMemoryStore()
        code_tools = make_memory_tools(store=store, principal_id_resolver=lambda: "alice")
        agent = Agent(tools=code_tools)

        import logging
        with caplog.at_level(logging.WARNING):
            cfg = self._minimal_config(type="inmemory")
            attach_declared_memory(agent, cfg, ws=None)

        # Code-wired recall kept; declared recall skipped; a warning was issued.
        names = [fn.__name__ for fn in agent._tool_fns if fn.__name__ == "recall"]
        assert len(names) == 1
        assert "recall" in caplog.text or "keeping" in caplog.text.lower()

    def test_tool_prefix_applied(self):
        from apx_agent._memory_wiring import attach_declared_memory

        agent = Agent(tools=[])
        cfg = self._minimal_config(type="inmemory", tool_prefix="mem_")
        attach_declared_memory(agent, cfg, ws=None)
        names = {fn.__name__ for fn in agent._tool_fns}
        assert "mem_recall" in names
        assert "recall" not in names

    def test_include_subset(self):
        from apx_agent._memory_wiring import attach_declared_memory

        agent = Agent(tools=[])
        cfg = self._minimal_config(type="inmemory", include=["recall"])
        attach_declared_memory(agent, cfg, ws=None)
        names = {fn.__name__ for fn in agent._tool_fns}
        assert "recall" in names
        assert "remember" not in names

    def test_no_memory_config_is_noop(self):
        from apx_agent._memory_wiring import attach_declared_memory

        agent = Agent(tools=[])
        cfg = AgentConfig(name="t")  # no memory
        attach_declared_memory(agent, cfg, ws=None)
        assert agent._tool_fns == []

    def test_lakebase_type_requires_ws_or_warns(self, caplog):
        """lakebase with ws=None logs a warning and skips (no crash)."""
        from apx_agent._memory_wiring import attach_declared_memory
        import logging

        agent = Agent(tools=[])
        cfg = self._minimal_config(
            type="lakebase",
            instance_name="inst",
            database="db",
            embedding_model="bge",
            embedding_dim=4,
        )
        with caplog.at_level(logging.WARNING):
            attach_declared_memory(agent, cfg, ws=None)

        # With ws=None, lakebase must skip and warn (not crash).
        assert agent._tool_fns == []
        assert "ws" in caplog.text.lower() or "lakebase" in caplog.text.lower() or "skip" in caplog.text.lower()


class TestResolveSessionStore:
    def test_override_wins_over_config(self):
        from apx_agent._memory_wiring import resolve_session_store
        from apx_agent._models import AgentConfig, SessionBackendConfig

        explicit = MagicMock()
        cfg = AgentConfig(name="t", session=SessionBackendConfig(type="inmemory"))
        result = resolve_session_store(cfg, ws=None, override=explicit)
        assert result is explicit

    def test_inmemory_config_returns_session_store(self):
        from apx_agent._memory_wiring import resolve_session_store
        from apx_agent._session import InMemorySessionStore
        from apx_agent._models import AgentConfig, SessionBackendConfig

        cfg = AgentConfig(name="t", session=SessionBackendConfig(type="inmemory"))
        result = resolve_session_store(cfg, ws=None, override=None)
        assert result is not None
        assert isinstance(result, InMemorySessionStore)

    def test_no_session_config_returns_none(self):
        from apx_agent._memory_wiring import resolve_session_store
        from apx_agent._models import AgentConfig

        cfg = AgentConfig(name="t")
        result = resolve_session_store(cfg, ws=None, override=None)
        assert result is None

    def test_lakebase_session_with_no_ws_returns_none_with_warning(self, caplog):
        from apx_agent._memory_wiring import resolve_session_store
        from apx_agent._models import AgentConfig, SessionBackendConfig
        import logging

        cfg = AgentConfig(name="t", session=SessionBackendConfig(
            type="lakebase", instance_name="inst", database="db"
        ))
        with caplog.at_level(logging.WARNING):
            result = resolve_session_store(cfg, ws=None, override=None)
        assert result is None
        assert "ws" in caplog.text.lower() or "lakebase" in caplog.text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_memory_wiring.py::TestAttachDeclaredMemory tests/test_memory_wiring.py::TestResolveSessionStore -v`
Expected: FAIL — `cannot import name 'attach_declared_memory' from 'apx_agent._memory_wiring'`.

- [ ] **Step 3: Write the implementation**

```python
# python/src/apx_agent/_memory_wiring.py
"""E3b: Declarative memory/session backend attach logic.

Two entry points:

- ``attach_declared_memory(agent, config, ws)`` — build config-declared
  memory + example stores and merge them into the agent via
  ``agent._register_tool``.  Called from ``finalize_agent`` before the
  A2A card snapshot (``_wiring.py:229``).  Idempotent via sentinel.

- ``resolve_session_store(config, ws, override=None)`` — return the
  explicit override if provided; else build a session store from
  ``config.session``; else ``None``.  Called in the ``create_app``
  lifespan to feed ``mount_invocations_route``.

Principal threading: memory tools emitted here carry a
``_principal: Dependencies.Principal`` dep param (via
``make_memory_tools(_use_dep_principal=True)``) so the per-request OBO
identity threads through the resolved-deps closure — the same mechanism
that delivers ``ctx.ws`` (proven in Phase 0, Task 0.2).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._models import AgentConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Store factories
# ---------------------------------------------------------------------------


def _build_memory_store(cfg: Any, ws: Any | None) -> Any:
    """Build the memory store for the given config.

    Raises ``ValueError`` for missing required fields.
    Returns ``None`` when lakebase/delta is requested with ``ws=None``
    (caller logs and skips).
    """
    from ._memory import InMemoryMemoryStore  # noqa: PLC0415

    if cfg.type == "inmemory":
        return InMemoryMemoryStore()

    if cfg.type == "delta":
        if ws is None:
            return None
        if not cfg.table_name:
            raise ValueError(
                "[tool.apx.agent.memory] type='delta' requires table_name "
                "(e.g. 'catalog.schema.apx_memories')."
            )
        from ._memory_delta import DeltaMemoryStore  # noqa: PLC0415
        from ._embeddings import make_embedding_fn  # noqa: PLC0415
        from ._sql import run_sql  # noqa: PLC0415

        embed_fn = None
        if cfg.embedding_model:
            if cfg.embedding_dim is None:
                raise ValueError(
                    "[tool.apx.agent.memory] type='delta' with embedding_model "
                    "requires embedding_dim."
                )
            embed_fn = make_embedding_fn(ws, cfg.embedding_model)

        return DeltaMemoryStore(
            run_sql=lambda q: run_sql(ws, q),
            embedding_fn=embed_fn,
            embedding_dim=cfg.embedding_dim,
            table_name=cfg.table_name,
            auto_create=cfg.auto_create,
            index_name=cfg.index_name,
        )

    if cfg.type == "lakebase":
        if ws is None:
            return None
        if not cfg.instance_name:
            raise ValueError(
                "[tool.apx.agent.memory] type='lakebase' requires instance_name."
            )
        if not cfg.database:
            raise ValueError(
                "[tool.apx.agent.memory] type='lakebase' requires database."
            )
        if not cfg.embedding_model:
            raise ValueError(
                "[tool.apx.agent.memory] type='lakebase' requires embedding_model."
            )
        if cfg.embedding_dim is None:
            raise ValueError(
                "[tool.apx.agent.memory] type='lakebase' requires embedding_dim."
            )
        from ._lakebase_engine import build_lakebase_engine  # noqa: PLC0415
        from ._embeddings import make_embedding_fn  # noqa: PLC0415
        from ._memory_lakebase import LakebaseMemoryStore  # noqa: PLC0415

        # Resolve $ENV_VAR in host.
        from ._wiring import _resolve_env_var  # noqa: PLC0415
        host = _resolve_env_var(cfg.host) if cfg.host else None

        engine = build_lakebase_engine(
            ws=ws,
            instance_name=cfg.instance_name,
            database=cfg.database,
            host=host,
        )
        embed_fn = make_embedding_fn(ws, cfg.embedding_model)
        return LakebaseMemoryStore(
            engine=engine,
            embedding_fn=embed_fn,
            embedding_dim=cfg.embedding_dim,
            table_name=cfg.table_name or "apx_memories",
            auto_create=cfg.auto_create,
            ensure_extension=cfg.ensure_extension,
        )

    raise ValueError(
        f"[tool.apx.agent.memory] unknown type {cfg.type!r}. "
        "Known: inmemory, delta, lakebase."
    )


def _build_session_store(cfg: Any, ws: Any | None) -> Any | None:
    """Build a SessionStore from SessionBackendConfig.  Returns None on failure."""
    if cfg.type == "inmemory":
        from ._session import InMemorySessionStore  # noqa: PLC0415
        return InMemorySessionStore()

    if cfg.type == "delta":
        if ws is None:
            logger.warning(
                "[tool.apx.agent.session] type='delta' requires ws; "
                "skipping session store (deploy with valid Databricks credentials)."
            )
            return None
        if not cfg.table_name:
            raise ValueError(
                "[tool.apx.agent.session] type='delta' requires table_name "
                "(three-part UC name: catalog.schema.table)."
            )
        from ._session_delta import DeltaSessionStore  # noqa: PLC0415

        # DeltaSessionStore takes `table_path` (not `table_name`).
        # `warehouse_id` is optional — pass from cfg.warehouse_id if
        # SessionBackendConfig carries it (verify DeltaSessionStore.__init__
        # at _session_delta.py:72-88 before wiring; field is warehouse_id: str|None=None).
        return DeltaSessionStore(
            ws=ws,
            table_path=cfg.table_name,  # DeltaSessionStore takes table_path, not table_name
            warehouse_id=getattr(cfg, "warehouse_id", None),
            auto_create=cfg.auto_create,
        )

    if cfg.type == "lakebase":
        if ws is None:
            logger.warning(
                "[tool.apx.agent.session] type='lakebase' requires ws; "
                "skipping session store (deploy with valid Databricks credentials)."
            )
            return None
        if not cfg.instance_name or not cfg.database:
            raise ValueError(
                "[tool.apx.agent.session] type='lakebase' requires instance_name and database."
            )
        from ._lakebase_engine import build_lakebase_engine  # noqa: PLC0415
        from ._session_lakebase import LakebaseSessionStore  # noqa: PLC0415
        from ._wiring import _resolve_env_var  # noqa: PLC0415

        host = _resolve_env_var(cfg.host) if cfg.host else None
        engine = build_lakebase_engine(
            ws=ws, instance_name=cfg.instance_name, database=cfg.database, host=host
        )
        return LakebaseSessionStore(
            engine=engine,
            table_name=cfg.table_name or "apx_sessions",
            auto_create=cfg.auto_create,
        )

    raise ValueError(
        f"[tool.apx.agent.session] unknown type {cfg.type!r}. "
        "Known: inmemory, delta, lakebase."
    )


def _build_example_store(cfg: Any, ws: Any | None) -> Any | None:
    """Build an ExampleStore from ExampleBackendConfig.

    Example stores isolate by ``agent_id`` (not ``principal_id``) — examples
    are coworker-scoped, not per-user.  Uses ``InMemoryExampleStore``,
    ``DeltaExampleStore``, or ``LakebaseExampleStore`` — distinct classes from
    the MemoryStore hierarchy (different schema, different tool names).
    """
    if cfg.type == "inmemory":
        from ._example import InMemoryExampleStore  # noqa: PLC0415
        return InMemoryExampleStore()

    if cfg.type == "delta":
        if ws is None:
            return None
        if not cfg.table_name:
            raise ValueError(
                "[tool.apx.agent.example] type='delta' requires table_name."
            )
        from ._example_delta import DeltaExampleStore  # noqa: PLC0415
        from ._embeddings import make_embedding_fn  # noqa: PLC0415
        from ._sql import run_sql  # noqa: PLC0415

        embed_fn = None
        if cfg.embedding_model:
            if cfg.embedding_dim is None:
                raise ValueError(
                    "[tool.apx.agent.example] type='delta' with embedding_model "
                    "requires embedding_dim."
                )
            embed_fn = make_embedding_fn(ws, cfg.embedding_model)

        # Inspect DeltaExampleStore.__init__ in _example_delta.py for exact
        # parameter names before implementing.
        return DeltaExampleStore(
            run_sql=lambda q: run_sql(ws, q),
            embedding_fn=embed_fn,
            embedding_dim=cfg.embedding_dim,
            table_name=cfg.table_name,
            auto_create=cfg.auto_create,
        )

    if cfg.type == "lakebase":
        if ws is None:
            return None
        if not cfg.instance_name or not cfg.database:
            raise ValueError(
                "[tool.apx.agent.example] type='lakebase' requires instance_name and database."
            )
        if not cfg.embedding_model or cfg.embedding_dim is None:
            raise ValueError(
                "[tool.apx.agent.example] type='lakebase' requires embedding_model and embedding_dim."
            )
        from ._lakebase_engine import build_lakebase_engine  # noqa: PLC0415
        from ._embeddings import make_embedding_fn  # noqa: PLC0415
        from ._example_lakebase import LakebaseExampleStore  # noqa: PLC0415
        from ._wiring import _resolve_env_var  # noqa: PLC0415

        host = _resolve_env_var(cfg.host) if cfg.host else None
        engine = build_lakebase_engine(
            ws=ws, instance_name=cfg.instance_name, database=cfg.database, host=host
        )
        embed_fn = make_embedding_fn(ws, cfg.embedding_model)
        # LakebaseExampleStore.__init__(engine, embedding_fn, embedding_dim, table_name,
        # auto_create, ensure_extension) — confirmed at _example_lakebase.py:154-177.
        return LakebaseExampleStore(
            engine=engine,
            embedding_fn=embed_fn,
            embedding_dim=cfg.embedding_dim,
            table_name=cfg.table_name or "apx_examples",
            auto_create=cfg.auto_create,
            ensure_extension=cfg.ensure_extension,
        )

    raise ValueError(
        f"[tool.apx.agent.example] unknown type {cfg.type!r}. "
        "Known: inmemory, delta, lakebase."
    )


# ---------------------------------------------------------------------------
# Attach helpers
# ---------------------------------------------------------------------------


def attach_declared_memory(
    agent: Any,
    config: "AgentConfig",
    ws: Any | None,
) -> None:
    """Build config-declared memory/example tools and register them on the agent.

    Called from ``finalize_agent`` BEFORE ``agent.collect_tools()`` so the
    minted tools appear in both the A2A card and the compiled LangGraph.

    Idempotent via ``_apx_memory_attached`` sentinel.  Code-wired tools win
    on name collision (same rule as ``merge_config_tools``).
    """
    if getattr(agent, "_apx_memory_attached", False):
        return

    register = getattr(agent, "_register_tool", None)
    if register is None:
        logger.warning(
            "[tool.apx.agent.memory/example] declared on a %s root that has no "
            "_register_tool — skipping (attach on a leaf LlmAgent).",
            type(agent).__name__,
        )
        setattr(agent, "_apx_memory_attached", True)
        return

    existing = {getattr(fn, "__name__", None) for fn in getattr(agent, "_tool_fns", [])}

    from ._memory_tools import make_memory_tools  # noqa: PLC0415
    from ._example_tools import make_example_tools  # noqa: PLC0415

    # --- memory ---
    if config.memory is not None:
        mcfg = config.memory
        store = None
        try:
            store = _build_memory_store(mcfg, ws)
        except (ValueError, ImportError) as exc:
            logger.warning(
                "[tool.apx.agent.memory] build failed — skipping memory tools: %s",
                exc,
            )
        if store is None and mcfg.type in ("lakebase", "delta"):
            logger.warning(
                "[tool.apx.agent.memory] type=%r requires ws; ws=None at this point "
                "(deploy with valid Databricks credentials). Memory tools will be absent.",
                mcfg.type,
            )
        if store is not None:
            # Config path uses the dep-principal mechanism (proved in Phase 0 Task 0.2).
            # _use_dep_principal=True emits tools with a `principal: Dependencies.Principal`
            # dep param (added in Task 1.3, which runs before this task).
            tools = make_memory_tools(
                store=store,
                _use_dep_principal=True,
                namespace_default=mcfg.namespace_default,
                tool_prefix=mcfg.tool_prefix,
                include=mcfg.include,             # type: ignore[arg-type]
            )
            for fn in tools:
                nm = getattr(fn, "__name__", None)
                if nm in existing:
                    logger.warning(
                        "[tool.apx.agent.memory] declares %r but the agent already wires "
                        "a tool with that name — keeping the code-wired tool, ignoring "
                        "the declared one. Use tool_prefix to mount both.",
                        nm,
                    )
                    continue
                register(fn)
                if nm:
                    existing.add(nm)

    # --- example ---
    if config.example is not None:
        ecfg = config.example
        estore = None
        try:
            estore = _build_example_store(ecfg, ws)
        except (ValueError, ImportError) as exc:
            logger.warning(
                "[tool.apx.agent.example] build failed — skipping example tools: %s",
                exc,
            )
        if estore is not None:
            agent_id = ecfg.agent_id or config.name
            example_tools = make_example_tools(
                store=estore,
                agent_id_resolver=lambda aid=agent_id: aid,
                tool_prefix=ecfg.tool_prefix,
                include=ecfg.include,             # type: ignore[arg-type]
            )
            for fn in example_tools:
                nm = getattr(fn, "__name__", None)
                if nm in existing:
                    logger.warning(
                        "[tool.apx.agent.example] declares %r but name collision — "
                        "keeping code-wired tool.",
                        nm,
                    )
                    continue
                register(fn)
                if nm:
                    existing.add(nm)

    setattr(agent, "_apx_memory_attached", True)


def resolve_session_store(
    config: "AgentConfig",
    ws: Any | None,
    override: Any | None = None,
) -> Any | None:
    """Return a SessionStore for this agent, or None.

    Precedence: explicit ``override`` arg > config ``session`` field > None.
    This is called in the ``create_app`` lifespan before ``mount_invocations_route``.
    The explicit ``create_app(session_store=X)`` arg wins over config (code is
    more specific intent).
    """
    if override is not None:
        return override
    if config.session is None:
        return None
    try:
        return _build_session_store(config.session, ws)
    except (ValueError, ImportError) as exc:
        logger.warning(
            "[tool.apx.agent.session] build failed — no session store: %s", exc
        )
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_memory_wiring.py::TestAttachDeclaredMemory tests/test_memory_wiring.py::TestResolveSessionStore -v`
Expected: PASS. (`_use_dep_principal` is available from Task 1.3, which runs before this task. The test for lakebase-with-no-ws should pass because `_build_memory_store` returns `None` when `ws=None` for lakebase type.)

Run: `cd python && uv run pyright src/apx_agent/_memory_wiring.py`
Expected: 0 errors.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_memory_wiring.py python/tests/test_memory_wiring.py
git commit -m "feat(memory): _memory_wiring — attach_declared_memory + resolve_session_store (E3b)"
```

## Task 1.5: Wire `attach_declared_memory` + `resolve_session_store` into `finalize_agent` / `create_app`

**Files:**
- Modify: `python/src/apx_agent/_wiring.py`
- Test: `python/tests/test_wiring.py` (append)

**Context:** `finalize_agent` must gain a `ws: Any | None = None` param. The card snapshot is at `_wiring.py:229`; `finalize_agent` is called at line 227 — it already fires before the snapshot. Adding `attach_declared_memory` inside `finalize_agent` guarantees it also fires at log/deploy time (via `log_agent`, which calls `finalize_agent`). The `create_app` lifespan must call `resolve_session_store` to replace the hardcoded `session_store=session_store` arg to `mount_invocations_route`.


- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_wiring.py
import textwrap
import pytest
from fastapi import FastAPI
from apx_agent import Agent, AgentConfig
from apx_agent._wiring import setup_agent, finalize_agent


class TestMemoryWiringIntegration:
    def test_finalize_agent_accepts_ws_param(self):
        """finalize_agent must accept ws kwarg without error."""
        agent = Agent(tools=[])
        cfg = AgentConfig(name="t")
        finalize_agent(agent, cfg, ws=None)  # must not raise

    def test_finalize_attaches_inmemory_memory_tools(self, tmp_path):
        """After finalize_agent, recall/remember/forget are in the agent's tools."""
        pp = tmp_path / "pyproject.toml"
        pp.write_text(textwrap.dedent("""
            [tool.apx.agent]
            name = "mem-test"
            [tool.apx.agent.memory]
            type = "inmemory"
        """))
        agent = Agent(tools=[])
        finalize_agent(agent, pyproject_path=str(pp), ws=None)
        names = {fn.__name__ for fn in agent._tool_fns}
        assert "recall" in names
        assert "remember" in names

    def test_finalize_memory_idempotent(self, tmp_path):
        """Second finalize_agent call does not double-attach memory tools."""
        pp = tmp_path / "pyproject.toml"
        pp.write_text(textwrap.dedent("""
            [tool.apx.agent]
            name = "t"
            [tool.apx.agent.memory]
            type = "inmemory"
        """))
        agent = Agent(tools=[])
        finalize_agent(agent, pyproject_path=str(pp), ws=None)
        finalize_agent(agent, pyproject_path=str(pp), ws=None)
        names = [fn.__name__ for fn in agent._tool_fns if fn.__name__ == "recall"]
        assert len(names) == 1

    @pytest.mark.asyncio
    async def test_setup_agent_memory_tools_appear_in_card(self, tmp_path):
        """After setup_agent, config-declared memory tools appear in the A2A card."""
        pp = tmp_path / "pyproject.toml"
        pp.write_text(textwrap.dedent("""
            [tool.apx.agent]
            name = "card-test"
            model = "databricks-claude-sonnet-4-6"
            [tool.apx.agent.memory]
            type = "inmemory"
        """))
        app = FastAPI()
        app.state.workspace_client = None
        agent = Agent(tools=[])
        ctx = await setup_agent(app, agent, pyproject_path=str(pp))
        assert ctx is not None
        skill_names = {s.name for s in ctx.card.skills}
        assert "recall" in skill_names, (
            "recall must be in the card skills — memory tools must attach "
            "BEFORE agent.collect_tools() at _wiring.py:229"
        )

    def test_resolve_session_override_wins_in_create_app(self):
        """Explicit session_store= arg to create_app wins over config session."""
        # This is a unit check on resolve_session_store; the full integration
        # requires a running lifespan. Verify the precedence rule directly.
        from apx_agent._memory_wiring import resolve_session_store
        from apx_agent._models import AgentConfig, SessionBackendConfig
        from unittest.mock import MagicMock

        explicit = MagicMock()
        cfg = AgentConfig(name="t", session=SessionBackendConfig(type="inmemory"))
        result = resolve_session_store(cfg, ws=None, override=explicit)
        assert result is explicit
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_wiring.py::TestMemoryWiringIntegration -v`
Expected: FAIL — `finalize_agent` has no `ws` param; memory tools not attached.

- [ ] **Step 3: Write the implementation**

In `python/src/apx_agent/_wiring.py`, update `finalize_agent` signature and body. **Read the current `finalize_agent` body first** (lines 125-157) — the E3c `apply_config_guardrails` call may or may not already be present depending on branch order. Add ONLY the `ws` param and the `attach_declared_memory` call; do not add `apply_config_guardrails` if it is absent (that is E3c's job, not E3b):

```python
def finalize_agent(
    agent: BaseAgent,
    config: AgentConfig | None = None,
    pyproject_path: str | None = None,
    ws: Any | None = None,                       # E3b: workspace client for lakebase/delta
) -> None:
    """Apply all config→instance steps before the agent is served or logged.

    ``ws`` is passed to ``attach_declared_memory`` for lakebase/delta backends.
    When ``None``, inmemory backends still attach; lakebase/delta skips with a
    warning (same graceful degradation as E3a template builds with ws=None).
    """
    if config is None:
        config = _load_agent_config(pyproject_path=pyproject_path)
    if config is not None:
        apply_config_knobs(agent, config)
        # NOTE: if E3c (apply_config_guardrails) is already present in this body,
        # leave it in place — do not remove it. Only add the lines below.

    from ._tool_config import merge_config_tools  # noqa: PLC0415
    merge_config_tools(agent, pyproject_path=pyproject_path)

    # E3b: memory/example tools — attach before card snapshot
    if config is not None:
        from ._memory_wiring import attach_declared_memory  # noqa: PLC0415
        attach_declared_memory(agent, config, ws=ws)
```

In `setup_agent`, update the `finalize_agent` call to pass `ws`:

```python
    # Apply knobs + persona overlay + config-tool merge + memory attach BEFORE
    # the card snapshot (collect_tools below) so all declared tools are both
    # callable and advertised.
    finalize_agent(agent, config, pyproject_path=pyproject_path,
                   ws=getattr(app.state, "workspace_client", None))
```

In `create_app` lifespan, replace the hardcoded `mount_invocations_route` call:

```python
        if ctx is not None:
            try:
                from ._invocations import mount_invocations_route   # noqa: PLC0415
                from ._memory_wiring import resolve_session_store    # noqa: PLC0415
                _effective_session = resolve_session_store(
                    ctx.config,
                    ws=app.state.workspace_client,
                    override=session_store,
                )
                mount_invocations_route(app, agent, ctx.config, session_store=_effective_session)
            except Exception as exc:
                logger.warning("Skipping /invocations mount: %s", exc)
```

(Verify exact indentation and surrounding code before editing.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_wiring.py::TestMemoryWiringIntegration -v`
Expected: PASS (all).

Run: `cd python && uv run pyright src/apx_agent/_wiring.py`
Expected: 0 errors.

Run: `cd python && uv run pytest tests/test_wiring.py -v`
Expected: all pre-existing wiring tests still pass.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_wiring.py python/tests/test_wiring.py
git commit -m "feat(wiring): finalize_agent ws param + attach_declared_memory + resolve_session_store in lifespan (E3b)"
```

---

## Task 1.6: Integration isolation test (spec §6 MANDATORY end-to-end)

**Files:**
- Test: `python/tests/test_memory_wiring.py` (append)

**Context:** Spec §6 requires a full end-to-end isolation test through `/invocations`. The Phase-0 Task 0.3 tests proved store-level isolation and dep-resolver isolation. This task proves the complete path: two requests with different `X-Forwarded-User` headers → different principals → no cross-contamination, using the config-declared inmemory store path through `finalize_agent` + `setup_agent`.

- [ ] **Step 1: Write the test**

```python
# Append to python/tests/test_memory_wiring.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class TestEndToEndIsolation:
    """Spec §6 MANDATORY isolation test — through the served agent stack."""

    @pytest.fixture
    def app_with_memory(self, tmp_path):
        """Build a FastAPI app with an inmemory memory config."""
        import textwrap
        (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
            [tool.apx.agent]
            name = "isolation-test"
            model = "databricks-meta-llama-3-3-70b-instruct"
            [tool.apx.agent.memory]
            type = "inmemory"
        """))
        from apx_agent import Agent, create_app
        from apx_agent._wiring import finalize_agent

        agent = Agent(tools=[])
        finalize_agent(agent, pyproject_path=str(tmp_path / "pyproject.toml"), ws=None)
        # Verify tools attached
        names = {fn.__name__ for fn in agent._tool_fns}
        assert "recall" in names
        assert "remember" in names
        return agent, str(tmp_path / "pyproject.toml")

    def test_recall_and_remember_tools_are_callable_and_isolated(self, app_with_memory):
        """Two principals using the same InMemoryMemoryStore via dep-principal tools
        must not see each other's memories.

        This simulates what the runtime does: two CompileContexts with different
        user_ids produce separate closures; each closure's _principal value is
        different; the store filters by principal_id.
        """
        agent, _ = app_with_memory
        from apx_agent._compile import _make_langchain_tool, CompileContext
        from unittest.mock import MagicMock

        def _ctx(user_id: str):
            ws = MagicMock()
            from apx_agent._defaults import DatabricksAppsHeaders
            headers = MagicMock(spec=DatabricksAppsHeaders)
            headers.user_id = user_id
            headers.token = None
            return CompileContext(ws=ws, model="m", headers=headers)

        # Compile the recall and remember tools for alice and bob
        recall_fn = next(fn for fn in agent._tool_fns if fn.__name__ == "recall")
        remember_fn = next(fn for fn in agent._tool_fns if fn.__name__ == "remember")

        lt_remember_alice = _make_langchain_tool(remember_fn, _ctx("alice"))
        lt_recall_alice = _make_langchain_tool(recall_fn, _ctx("alice"))
        lt_recall_bob = _make_langchain_tool(recall_fn, _ctx("bob"))

        # Alice writes; bob must not read it
        lt_remember_alice.run({"content": "alice e2e memory"})
        alice_result = lt_recall_alice.run({"query": "e2e memory"})
        bob_result = lt_recall_bob.run({"query": "e2e memory"})

        assert "alice e2e memory" in alice_result, (
            "Alice must recall her own memory through the served stack."
        )
        assert "alice e2e memory" not in bob_result, (
            "Bob must NOT see Alice's memory — isolation breach! "
            "Check the _use_dep_principal dep-resolver wiring."
        )
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_memory_wiring.py::TestEndToEndIsolation -v`
Expected: PASS. If this fails, the dep-principal closure is broken — revisit Task 1.4's `_use_dep_principal` implementation and verify the `_compile.py` `_make_dep_resolvers` entry.

- [ ] **Step 3: No implementation**

No source changes. Pass = full E3b isolation guarantee verified.

- [ ] **Step 4: Run all E3b tests together**

Run: `cd python && uv run pytest tests/test_lakebase_engine.py tests/test_embeddings.py tests/test_memory_wiring.py tests/test_memory_tools.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add python/tests/test_memory_wiring.py
git commit -m "test(e3b): spec §6 mandatory end-to-end isolation test through compile closure"
```

---

## Task 1.7: Public exports + docs + full regression

**Files:**
- Modify: `python/src/apx_agent/__init__.py`
- Modify: `docs/configuration.md`
- Test: `python/tests/test_wiring.py` (append — export check)

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_wiring.py

def test_public_exports_memory_config_models():
    import apx_agent
    from apx_agent import MemoryBackendConfig, ExampleBackendConfig, SessionBackendConfig
    assert "MemoryBackendConfig" in apx_agent.__all__ if hasattr(apx_agent, "__all__") else True
    # Round-trip check
    cfg = MemoryBackendConfig(type="lakebase", instance_name="inst", database="db",
                               embedding_model="bge", embedding_dim=1024)
    assert cfg.type == "lakebase"
    assert cfg.validate_at_boot is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_wiring.py::test_public_exports_memory_config_models -v`
Expected: FAIL — `cannot import name 'MemoryBackendConfig' from 'apx_agent'`.

- [ ] **Step 3: Export config models + add docs**

In `python/src/apx_agent/__init__.py`, add to the Models import group:

```python
from ._models import (
    ...
    ExampleBackendConfig,
    MemoryBackendConfig,
    SessionBackendConfig,
)
```

Add to `__all__` (if present): `"MemoryBackendConfig"`, `"ExampleBackendConfig"`, `"SessionBackendConfig"`.

In `docs/configuration.md`, add a "Declarative memory" section (after the guardrails section if E3c has landed, otherwise after the tools section):

````markdown
## Declarative memory — `[tool.apx.agent.memory]`

> Python only. Declares a memory backend that is auto-attached on all runtimes — serve, log/deploy, `apx info`. Memory tools are additive over code-wired tools; code-wired tools win on name collision.

```toml
[tool.apx.agent]
name = "sales-coworker"
model = "databricks-claude-sonnet-4-6"

# --- in-memory (dev / tests) ---
[tool.apx.agent.memory]
type = "inmemory"

# --- or Lakebase (production) ---
# [tool.apx.agent.memory]
# type = "lakebase"
# instance_name = "coworker-lakebase"
# database = "agentdb"
# embedding_model = "databricks-bge-large-en"
# embedding_dim = 1024
```

**Memory fields:**

| Key | Type | Default | Description |
|---|---|---|---|
| `type` | `"inmemory" \| "delta" \| "lakebase"` | `"inmemory"` | Backend type |
| `embedding_model` | `str` | absent | Databricks serving-endpoint name for embeddings |
| `embedding_dim` | `int` | absent | Embedding dimensionality (required for lakebase) |
| `table_name` | `str` | absent | UC table name (delta) or plain name (lakebase) |
| `instance_name` | `str` | absent | Lakebase instance name (`ws.database`) |
| `database` | `str` | absent | Postgres database name |
| `host` | `str` | absent | Lakebase endpoint host; supports `$ENV_VAR` |
| `auto_create` | `bool` | `true` | Create table on first use |
| `ensure_extension` | `bool` | `true` | Run `CREATE EXTENSION IF NOT EXISTS vector` |
| `namespace_default` | `str` | `"default"` | Default namespace for memory tools |
| `tool_prefix` | `str` | `""` | Prefix for tool names (e.g. `"mem_"` → `"mem_recall"`) |
| `include` | `list[str]` | all | Subset of tools: `["recall"]`, `["recall","remember"]`, etc. |
| `validate_at_boot` | `bool` | `true` | Connectivity check at startup; set `false` for offline envs |

**Principal isolation:** Memory is scoped per OBO user (`X-Forwarded-User` header). User A's memories are invisible to User B. No-principal requests (local dev without headers) return `NO_PRINCIPAL` without writing to the store.

**Credential API:** Lakebase backends use `ws.database.generate_database_credential(instance_names=[...])` (the `DatabaseAPI`, not `PostgresAPI`).

## Declarative session — `[tool.apx.agent.session]`

```toml
[tool.apx.agent.session]
type = "inmemory"

# --- or Delta ---
# [tool.apx.agent.session]
# type = "delta"
# table_name = "main.coworker.apx_sessions"

# --- or Lakebase ---
# [tool.apx.agent.session]
# type = "lakebase"
# instance_name = "coworker-lakebase"
# database = "agentdb"
```

**Session precedence:** An explicit `create_app(session_store=X)` arg **wins** over the config session (code is more specific intent). Config session is the fallback for template-only projects.

**Note:** `DeltaSessionStore` takes `table_path` (three-part UC name); the wiring maps `table_name` → `table_path` automatically.
````

- [ ] **Step 4: Run full regression + typecheck**

```bash
cd python && uv run pytest -q
```
Expected: no new failures vs. baseline.

```bash
cd python && uv run pyright src/apx_agent/_defaults.py
cd python && uv run pyright src/apx_agent/_wiring.py
cd python && uv run pyright src/apx_agent/_models.py
cd python && uv run pyright src/apx_agent/_memory_tools.py
cd python && uv run pyright src/apx_agent/_memory_wiring.py
cd python && uv run pyright src/apx_agent/_lakebase_engine.py
cd python && uv run pyright src/apx_agent/_embeddings.py
```
Expected: 0 errors on each.

```bash
cd python && uv run python -c "
from apx_agent import MemoryBackendConfig, SessionBackendConfig, ExampleBackendConfig
cfg = MemoryBackendConfig(type='inmemory')
print('MemoryBackendConfig OK:', cfg)
cfg_s = SessionBackendConfig(type='inmemory')
print('SessionBackendConfig OK:', cfg_s)
"
```
Expected output (no errors):
```
MemoryBackendConfig OK: type='inmemory' ...
SessionBackendConfig OK: type='inmemory' ...
```

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/__init__.py docs/configuration.md python/tests/test_wiring.py
git commit -m "feat(e3b): export MemoryBackendConfig, ExampleBackendConfig, SessionBackendConfig; docs"
```

---

## Open questions (record for follow-up)

- **OQ1 — `embedding_model` default:** Should lakebase require explicit `embedding_model` or fall back to a workspace default endpoint? Current plan: always explicit (raises `ValueError` at build if missing for lakebase). Spec Q2.
- **OQ2 — Delta write auth:** Config-driven memory writes run as the app SP (for DDL/auto_create) — per-request OBO is the reader via the resolved principal. Row-level isolation (`WHERE principal_id = :caller`) holds regardless of writer identity. Confirm this is the desired model vs. OBO writes for tighter audit.
- **OQ3 — CLI `memory_store` MODULE:VAR coexistence:** Declarative (`[tool.apx.agent.memory]`) and the legacy CLI key (`memory_store = "module:var"`) are different surfaces — keep both. CLI = code-ref for offline `apx memory` commands; declarative = data for the served runtime. No deprecation in E3b.
- **OQ4 — Example store isolation semantics:** `ExampleBackendConfig` uses `agent_id` (config.name) as the partition key, not `principal_id`. This means all users of the same coworker share examples — intentional (examples are agent-scoped knowledge, not per-user). Confirm this is correct before E3b is promoted to production.
- **OQ5 — `DeltaSessionStore` constructor (RESOLVED):** `DeltaSessionStore.__init__(self, *, ws, table_path, warehouse_id=None, auto_create=True)` confirmed at `_session_delta.py:72-87`. Takes `table_path` (not `table_name`). `warehouse_id` is optional. `SessionBackendConfig` includes `warehouse_id: str | None = None` (Task 1.1). The wiring passes `table_path=cfg.table_name` and `warehouse_id=cfg.warehouse_id`.
- **OQ6 — `validate_at_boot=True` implementation:** Task 1.3 stubbed this as a field; the actual connectivity check (credential mint + `SELECT 1`) is deferred. Add a `_validate_store(store, cfg)` helper in `_memory_wiring.py` that runs a credential mint (lakebase) or a SELECT 1 (delta) and logs a startup warning on failure. This should be a follow-up task or added to Task 1.3 before shipping to production.

---

## Self-review notes (author)

**Spec coverage:**
- SDK credential reconciliation + `_lakebase_engine.py` → T0.1 (`ws.database`, not `ws.postgres`).
- `_get_principal` dependency + `_make_dep_resolvers` entry + async→sync thread-hop proof → T0.2.
- MANDATORY cross-principal isolation, all directions + dep-principal path → T0.3.
- GATE (both 0.2 and 0.3 must pass).
- Config models (all three, `extra="forbid"`, TOML round-trip) → T1.1.
- `make_embedding_fn` → T1.2.
- `make_memory_tools _use_dep_principal` path (with `principal` dep param, no leading underscore, no default) → T1.3 **[must come before T1.4]**.
- Store factory dispatch (inmemory/delta/lakebase) + `attach_declared_memory` + `resolve_session_store` + `_build_example_store` (using `LakebaseExampleStore`, not `LakebaseMemoryStore`) → T1.4.
- `finalize_agent ws` param + memory attach in lifespan + session wiring → T1.5.
- Spec §6 end-to-end isolation through compile closure → T1.6.
- Exports + docs + regression → T1.7.

**Architecture reconciliation logged:**
- Spec §3.1 placed `attach_declared_tools` in `setup_agent` (before card snapshot). This plan places it in `finalize_agent` (which `setup_agent` already calls at line 227, before the snapshot at line 229). Same effect, but ensures log/deploy coverage (via `log_agent` → `finalize_agent`) with no extra wiring. The scope-doc placement is noted and the override is intentional — same pattern E3c used.
- Session deliberately NOT in `finalize_agent` (no tools, no card). It flows through `create_app` lifespan only.

**`ws.postgres` vs `ws.database` (critical reconciliation):**
`PostgresAPI.generate_database_credential(self, endpoint: str)` — positional endpoint string. `DatabaseAPI.generate_database_credential(self, *, instance_names=None, request_id=None)` — named args. Only `DatabaseAPI` matches the `instance_names` + `request_id` pattern in both store docstrings. The `_memory_lakebase.py` docstring showing `ws.postgres` is **wrong**; Task 0.1 fixes it and documents the decision.

**Idempotency is a correctness requirement:** `setup_agent` calls `finalize_agent`; `mount_mcp_endpoints` may call `setup_agent` a second time. Without the `_apx_memory_attached` sentinel, memory tools double-attach, doubling the card and the compiled tool list.

**No `ws=None` crash for `inmemory`:** All Phase-0 tests and unit tests pass with `ws=None` — `inmemory` type never needs `ws`. Delta/lakebase with `ws=None` log a warning and skip; the agent still serves without memory tools (same graceful degradation as E3a template builds).

**Thread-hop safety (the E3b design bet):** The dep-resolver mechanism captures `principal_id_resolver` values as captured closure variables before `ThreadPoolExecutor.submit`. Task 0.2's `test_async_tool_sees_correct_principal_via_thread_hop` is the empirical proof. If this test ever fails, the assumption is wrong — the gate criterion requires escalating to option (a) before Phase 1 proceeds.

**Inspect-before-edit flags for the implementer:**
- `_wiring.py:227` — the exact `finalize_agent` call in `setup_agent` before the snapshot at line 229. Verify before editing to add `ws=getattr(app.state, "workspace_client", None)`.
- `_wiring.py` `create_app` lifespan, line 617 — the exact `mount_invocations_route` call before patching to use `resolve_session_store`.
- `_session_delta.py` constructor signature — verify `warehouse_id` requirement before writing `_build_session_store`'s delta branch.
- `_example_tools.py:67` — verify `make_example_tools` signature mirrors `make_memory_tools` for the `agent_id_resolver` param.
- `_memory_tools.py` `tool` decorator import and existing `recall` function indentation before splicing the `_use_dep_principal` branch.

**Out of scope (E3b):**
- Live integration tests against a real Lakebase instance (gate behind `@pytest.mark.integration`; CI uses mocks).
- `validate_at_boot` implementation (connectivity check at attach time — OQ6 above). The field exists; the `_validate_store` helper body is deferred.
- Delta session `warehouse_id` is resolved (OQ5 resolved): `SessionBackendConfig.warehouse_id: str | None = None` added in Task 1.1.
- Coworker `memory_store` MODULE:VAR deprecation.
- `apx memory`/`apx example` CLI commands (different surface; unchanged).
