"""Reality check (ctk): durable Lakebase checkpointer wiring (#329 Slice C).

A pending mid-turn approval is checkpoint state, not a completed turn — so it
only survives a restart when a DURABLE checkpointer owns the thread. This proves
the *wiring* that selects one:

  * resolve_checkpointer builds a Lakebase PostgresSaver for a lakebase session
    (and returns None for every other backend, so the served agent keeps its
    in-process InMemorySaver default);
  * the mount functions thread that checkpointer into the served ChatAgent /
    ResponsesAgent;
  * build_lakebase_checkpointer fails with clear guidance when the extra is
    absent, and degrades to None (never crashes startup).

The durability PROPERTY itself (approval survives a fresh checkpointer instance)
is proven in test_approval_served_reality_ctk.py — here we prove the selection
reaches the served path. Real Lakebase persistence is exercised against a live
instance (langgraph-checkpoint-postgres is an optional extra, not in CI).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("mlflow")

from apx_agent import LlmAgent  # noqa: E402
from apx_agent._models import AgentConfig, SessionBackendConfig  # noqa: E402


def _lakebase_config() -> AgentConfig:
    return AgentConfig(
        name="t",
        model="m",
        session=SessionBackendConfig(
            type="lakebase", database="db", host="h.example"
        ),
    )


# ── resolve_checkpointer selection ────────────────────────────────────────────


def test_resolve_checkpointer_builds_postgres_saver_for_lakebase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apx_agent._checkpoint_lakebase as cp
    from apx_agent._memory_wiring import resolve_checkpointer

    captured: dict[str, Any] = {}
    sentinel = object()

    def fake_build(*, ws: Any, database: str, host: str) -> Any:
        captured.update(database=database, host=host)
        return sentinel

    monkeypatch.setattr(cp, "build_lakebase_checkpointer", fake_build)
    result = resolve_checkpointer(_lakebase_config(), ws=MagicMock())
    assert result is sentinel
    assert captured == {"database": "db", "host": "h.example"}


def test_resolve_checkpointer_none_for_non_lakebase() -> None:
    from apx_agent._memory_wiring import resolve_checkpointer

    delta = AgentConfig(
        name="t", model="m",
        session=SessionBackendConfig(type="delta", table_name="c.s.p"),
    )
    assert resolve_checkpointer(delta, ws=MagicMock()) is None
    assert resolve_checkpointer(AgentConfig(name="t", model="m"), ws=MagicMock()) is None


def test_resolve_checkpointer_none_for_composite_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#482: a composite agent + lakebase session must degrade to stateless, not
    build a PostgresSaver that _compile rejects (→ HTTP 500 every turn)."""
    import apx_agent._checkpoint_lakebase as cp
    from apx_agent import SequentialAgent
    from apx_agent._memory_wiring import resolve_checkpointer

    called = False

    def fake_build(**_: Any) -> Any:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(cp, "build_lakebase_checkpointer", fake_build)
    composite = SequentialAgent([LlmAgent(tools=[]), LlmAgent(tools=[])])
    result = resolve_checkpointer(_lakebase_config(), ws=MagicMock(), agent=composite)
    assert result is None  # stateless, no crash
    assert not called  # never even tries to build the saver


def test_resolve_checkpointer_builds_for_llm_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An LlmAgent + lakebase session still gets the durable checkpointer."""
    import apx_agent._checkpoint_lakebase as cp
    from apx_agent._memory_wiring import resolve_checkpointer

    sentinel = object()
    monkeypatch.setattr(cp, "build_lakebase_checkpointer", lambda **_: sentinel)
    result = resolve_checkpointer(
        _lakebase_config(), ws=MagicMock(), agent=LlmAgent(tools=[])
    )
    assert result is sentinel


def test_resolve_checkpointer_degrades_to_none_on_build_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apx_agent._checkpoint_lakebase as cp
    from apx_agent._memory_wiring import resolve_checkpointer

    def boom(**_: Any) -> Any:
        raise RuntimeError("lakebase unreachable")

    monkeypatch.setattr(cp, "build_lakebase_checkpointer", boom)
    # Unreachable Lakebase must not crash startup — fall back to in-process memory.
    assert resolve_checkpointer(_lakebase_config(), ws=MagicMock()) is None


def test_resolve_checkpointer_none_without_ws() -> None:
    from apx_agent._memory_wiring import resolve_checkpointer

    assert resolve_checkpointer(_lakebase_config(), ws=None) is None


def test_checkpointer_target_detects_expected_durable_checkpointer() -> None:
    """#490: the shared applicability predicate the create_app wiring uses to
    tell 'no checkpointer wanted' apart from 'wanted one but it failed to build'."""
    from apx_agent import SequentialAgent
    from apx_agent._memory_wiring import _lakebase_checkpointer_target

    # lakebase session + LlmAgent → a durable checkpointer IS expected.
    t = _lakebase_checkpointer_target(
        _lakebase_config(), MagicMock(), LlmAgent(tools=[]), None
    )
    assert t is not None
    assert t.host == "h.example"
    assert t.database == "db"

    # composite agent → stateless by design, not expected (not a degradation).
    composite = SequentialAgent([LlmAgent(tools=[]), LlmAgent(tools=[])])
    assert _lakebase_checkpointer_target(_lakebase_config(), MagicMock(), composite, None) is None

    # no lakebase session → in-process default, not expected.
    assert _lakebase_checkpointer_target(
        AgentConfig(name="t", model="m"), MagicMock(), LlmAgent(tools=[]), None
    ) is None

    # explicit conversation_store override → caller owns state, not expected.
    assert _lakebase_checkpointer_target(
        _lakebase_config(), MagicMock(), LlmAgent(tools=[]), object()
    ) is None


def test_resolve_checkpointer_none_when_store_override_given() -> None:
    # An explicit conversation_store override means the caller owns session
    # state — don't spin up an unrequested PostgresSaver from config.session.
    from apx_agent._memory_wiring import resolve_checkpointer

    assert resolve_checkpointer(
        _lakebase_config(), ws=MagicMock(), store_override=object()
    ) is None


# ── build_lakebase_checkpointer gating ────────────────────────────────────────


def test_build_checkpointer_requires_extra() -> None:
    from apx_agent._checkpoint_lakebase import build_lakebase_checkpointer

    try:
        import psycopg_pool  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("lakebase extra installed — ImportError path not exercised here")

    with pytest.raises(ImportError, match="lakebase"):
        build_lakebase_checkpointer(ws=MagicMock(), database="d", host="h.example")


# ── mount functions thread the checkpointer into the served agents ────────────


def test_mount_invocations_threads_checkpointer(monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import _chat_agent
    from apx_agent._invocations import mount_invocations_route
    from fastapi import FastAPI

    captured: dict[str, Any] = {}

    def fake(agent: Any, *, model: str, conversation_store: Any = None,
             agent_id: Any = None, checkpointer: Any = None) -> Any:
        captured["checkpointer"] = checkpointer
        return MagicMock()

    monkeypatch.setattr(_chat_agent, "chat_agent_for", fake)
    sentinel = object()
    ok = mount_invocations_route(
        FastAPI(), LlmAgent(name="t", tools=[]), AgentConfig(name="t", model="m"),
        checkpointer=sentinel,
    )
    assert ok and captured["checkpointer"] is sentinel


def test_mount_responses_threads_checkpointer(monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import _responses_agent
    from apx_agent._invocations import mount_responses_route
    from fastapi import FastAPI

    captured: dict[str, Any] = {}

    def fake(agent: Any, *, model: str, **kw: Any) -> Any:
        captured["checkpointer"] = kw.get("checkpointer")
        return (lambda r: None, lambda r: iter(()))  # (non_streaming, streaming)

    monkeypatch.setattr(_responses_agent, "compile_to_responses_agent", fake)
    sentinel = object()
    ok = mount_responses_route(
        FastAPI(), LlmAgent(name="t", tools=[]), AgentConfig(name="t", model="m"),
        checkpointer=sentinel,
    )
    assert ok and captured["checkpointer"] is sentinel


# ── close_checkpointer: the durable pool is released on shutdown (#346) ────────


def test_close_checkpointer_closes_the_pool() -> None:
    # A PostgresSaver's pool is its ``.conn`` (the ConnectionPool we opened).
    # Shutdown must close it or the pool + its connections + worker thread leak.
    from apx_agent._memory_wiring import close_checkpointer

    saver = MagicMock()
    close_checkpointer(saver)
    saver.conn.close.assert_called_once_with()


def test_close_checkpointer_noop_for_none() -> None:
    # In-process default owns no pool — closing None must not raise.
    from apx_agent._memory_wiring import close_checkpointer

    close_checkpointer(None)  # no raise


def test_close_checkpointer_swallows_close_error() -> None:
    # Shutdown must never raise, even if the pool's close() blows up.
    from apx_agent._memory_wiring import close_checkpointer

    saver = MagicMock()
    saver.conn.close.side_effect = RuntimeError("pool already gone")
    close_checkpointer(saver)  # no raise
    saver.conn.close.assert_called_once_with()


def test_lifespan_closes_checkpointer_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Prove the wiring, not just the helper: the FastAPI lifespan stashes the
    # resolved checkpointer on app.state and closes its pool when the app shuts
    # down. Uses a TestClient context manager, which runs startup + shutdown.
    from apx_agent import create_app
    from fastapi.testclient import TestClient

    saver = MagicMock()

    def fake_resolve(*_: Any, **__: Any) -> Any:
        return saver

    import apx_agent._memory_wiring as mw

    monkeypatch.setattr(mw, "resolve_checkpointer", fake_resolve)
    app = create_app(LlmAgent(name="t", tools=[]), AgentConfig(name="t", model="m"))
    with TestClient(app):
        pass  # startup mounts + stashes; exiting the block runs shutdown
    saver.conn.close.assert_called_once_with()
