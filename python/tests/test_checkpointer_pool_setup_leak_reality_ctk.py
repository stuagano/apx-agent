"""Claim-vs-reality: a failed checkpointer setup must not leak the pool (#377).

build_lakebase_checkpointer opens the ConnectionPool eagerly (open=True) but did
not wrap PostgresSaver(pool) / saver.setup() in try/finally. If setup() raises
(a transient Lakebase error or a role lacking DDL privilege), the exception
propagates with no saver returned — so the FastAPI lifespan's close_checkpointer
never gets a handle, and each startup/retry leaks a live psycopg pool + its
maintenance thread.

This reconciles the *claim* ("build the checkpointer or fail cleanly") against
reality: when setup() raises, the eagerly-opened pool is closed before the error
propagates. The lakebase extra (psycopg) isn't installed in the test env, so the
extra's modules are faked to exercise the failure path directly.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.mark.unit
def test_setup_failure_closes_the_pool(monkeypatch) -> None:
    # Fake the lakebase-extra modules so build_lakebase_checkpointer runs without
    # a real psycopg / Lakebase connection.
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.Connection = type(  # subclassable base for _LakebaseConnection
        "Connection", (), {"connect": classmethod(lambda cls, *a, **k: MagicMock())}
    )
    rows_mod = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = object()
    fake_psycopg.rows = rows_mod

    pool = MagicMock(name="pool")
    fake_pool_mod = types.ModuleType("psycopg_pool")
    fake_pool_mod.ConnectionPool = MagicMock(return_value=pool)

    class _BoomSaver:
        def __init__(self, *_a, **_k) -> None:
            pass

        def setup(self) -> None:
            raise RuntimeError("permission denied to create checkpoint tables")

    fake_lg = types.ModuleType("langgraph.checkpoint.postgres")
    fake_lg.PostgresSaver = _BoomSaver

    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_mod)
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_pool_mod)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", fake_lg)

    from apx_agent._checkpoint_lakebase import build_lakebase_checkpointer

    ws = MagicMock()
    ws.current_user.me.return_value.user_name = "u@example.com"

    with pytest.raises(RuntimeError, match="permission denied"):
        build_lakebase_checkpointer(ws=ws, database="agentdb", host="h.lakebase")

    pool.close.assert_called_once()  # the eagerly-opened pool was not leaked
