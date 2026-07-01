from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _mock_ws(token: str = "test-tok") -> Any:
    ws = MagicMock()
    # Lakebase takes a Databricks OAuth access token as the password.
    ws.config.authenticate.return_value = {"Authorization": f"Bearer {token}"}
    # The pg role is the authenticated principal, not a literal "apx-agent".
    ws.current_user.me.return_value = types.SimpleNamespace(user_name="me@databricks.com")
    return ws


def test_build_lakebase_engine_returns_engine():
    pytest.importorskip("psycopg")
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from apx_agent._lakebase_engine import build_lakebase_engine
    ws = _mock_ws()
    engine = build_lakebase_engine(ws=ws, database="agentdb", host="localhost")
    assert engine is not None
    assert hasattr(engine, "connect")
    assert hasattr(engine, "url")


def test_do_connect_listener_injects_oauth_token():
    pytest.importorskip("psycopg")
    pytest.importorskip("sqlalchemy")
    from apx_agent._lakebase_engine import build_lakebase_engine
    ws = _mock_ws("fresh-tok")
    engine = build_lakebase_engine(ws=ws, database="agentdb", host="testhost")
    # Fire the do_connect listener directly with a mutable ckwargs dict. The
    # listener is registered on the engine but dispatched through the dialect.
    ckwargs: dict = {}
    engine.dialect.dispatch.do_connect(engine.dialect, None, [], ckwargs)
    # The listener injects a fresh Databricks OAuth token as the password.
    ws.config.authenticate.assert_called()
    assert ckwargs["password"] == "fresh-tok"


def test_build_lakebase_engine_host_defaults_to_provided():
    pytest.importorskip("psycopg")
    pytest.importorskip("sqlalchemy")
    from apx_agent._lakebase_engine import build_lakebase_engine
    ws = _mock_ws()
    engine = build_lakebase_engine(ws=ws, database="db", host="myhost.example.com")
    assert "myhost.example.com" in str(engine.url)


def test_engine_connects_as_principal_and_requires_ssl():
    """The role is the authenticated principal (not 'apx-agent') and Lakebase's
    mandated SSL is set — the two bugs the live checkpointer smoke surfaced."""
    pytest.importorskip("psycopg")
    pytest.importorskip("sqlalchemy")
    from apx_agent._lakebase_engine import build_lakebase_engine
    engine = build_lakebase_engine(
        ws=_mock_ws(), database="db", host="h.example.com"
    )
    assert engine.url.username == "me@databricks.com"
    assert "apx-agent" not in str(engine.url)
    assert "sslmode=require" in str(engine.url)


