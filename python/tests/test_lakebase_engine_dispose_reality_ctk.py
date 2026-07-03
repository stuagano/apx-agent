"""Claim-vs-reality: sibling Lakebase engines are disposed on shutdown (#376).

#346 closed the checkpointer pool on shutdown but left the sibling Lakebase
engines open — the conversation store (resolve_conversation_store ->
build_lakebase_engine) and the declared memory/example stores. Each owns a
psycopg pool with a do_connect OAuth-token listener, so any path that rebuilds
the app in one process (pytest, uvicorn --reload, multi-app hosting) leaks a
live pool per cycle until Lakebase refuses new connections.

This reconciles the *claim* ("the runtime cleans up its resources on shutdown")
against reality: on lifespan shutdown, the conversation store's engine (and any
declared memory/example store engines) get disposed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from apx_agent import AgentConfig, LlmAgent, create_app
from apx_agent._memory_wiring import dispose_store_engine


def _tool(q: str) -> str:
    """Echo."""
    return q


@pytest.mark.unit
def test_dispose_store_engine_disposes_and_tolerates_missing() -> None:
    engine = MagicMock(name="engine")
    dispose_store_engine(SimpleNamespace(engine=engine))
    engine.dispose.assert_called_once()

    # No engine / None must be a safe no-op (Delta/in-memory stores, shutdown).
    dispose_store_engine(SimpleNamespace())  # no .engine attr
    dispose_store_engine(None)


@pytest.mark.unit
def test_conversation_store_engine_disposed_on_lifespan_shutdown(monkeypatch) -> None:
    import apx_agent._memory_wiring as mw

    engine = MagicMock(name="lakebase_engine")
    fake_store = SimpleNamespace(engine=engine)  # a Lakebase-like conversation store
    monkeypatch.setattr(mw, "resolve_conversation_store", lambda *_a, **_k: fake_store)
    monkeypatch.setattr(mw, "resolve_checkpointer", lambda *_a, **_k: None)

    from unittest.mock import patch

    with patch("apx_agent._wiring._make_workspace_client", return_value=MagicMock()):
        app = create_app(LlmAgent(tools=[_tool]), config=AgentConfig(name="t", model="m"))
        with TestClient(app):
            pass  # __enter__ runs startup (builds store), __exit__ runs shutdown

    engine.dispose.assert_called_once()
