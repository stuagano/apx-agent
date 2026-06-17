from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _mock_ws(token: str = "test-tok") -> Any:
    cred = types.SimpleNamespace(token=token)
    database_api = MagicMock()
    database_api.generate_database_credential.return_value = cred
    ws = MagicMock()
    ws.database = database_api
    return ws


def test_build_lakebase_engine_returns_engine():
    pytest.importorskip("psycopg")
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from apx_agent._lakebase_engine import build_lakebase_engine
    ws = _mock_ws()
    engine = build_lakebase_engine(ws=ws, instance_name="test-lakebase", database="agentdb", host="localhost")
    assert engine is not None
    assert hasattr(engine, "connect")
    assert hasattr(engine, "url")


def test_do_connect_listener_mints_and_injects_token():
    pytest.importorskip("psycopg")
    pytest.importorskip("sqlalchemy")
    from apx_agent._lakebase_engine import build_lakebase_engine
    ws = _mock_ws("fresh-tok")
    engine = build_lakebase_engine(ws=ws, instance_name="my-instance", database="agentdb", host="testhost")
    # Fire the do_connect listener directly with a mutable ckwargs dict. The
    # listener is registered on the engine but dispatched through the dialect.
    ckwargs: dict = {}
    engine.dialect.dispatch.do_connect(engine.dialect, None, [], ckwargs)
    # The listener must have minted a fresh token and injected it as the password.
    ws.database.generate_database_credential.assert_called_once_with(
        instance_names=["my-instance"], request_id="apx-agent-lakebase"
    )
    assert ckwargs["password"] == "fresh-tok"


def test_build_lakebase_engine_host_defaults_to_provided():
    pytest.importorskip("psycopg")
    pytest.importorskip("sqlalchemy")
    from apx_agent._lakebase_engine import build_lakebase_engine
    ws = _mock_ws()
    engine = build_lakebase_engine(ws=ws, instance_name="inst", database="db", host="myhost.example.com")
    assert "myhost.example.com" in str(engine.url)


def test_postgres_api_has_wrong_signature_for_instance_names():
    """Regression: PostgresAPI.generate_database_credential takes positional endpoint,
    NOT instance_names. DatabaseAPI takes instance_names+request_id (the correct one)."""
    import inspect
    from databricks.sdk.service.postgres import PostgresAPI
    from databricks.sdk.service.database import DatabaseAPI
    pg_sig = inspect.signature(PostgresAPI.generate_database_credential)
    db_sig = inspect.signature(DatabaseAPI.generate_database_credential)
    pg_params = list(pg_sig.parameters.keys())
    assert "endpoint" in pg_params
    assert "instance_names" not in pg_params
    db_params = list(db_sig.parameters.keys())
    assert "instance_names" in db_params
    assert "request_id" in db_params
