"""#1135 / ADR 0001: apx's Lakebase stores still build, but warn; doctor flags them."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apx_agent import AgentConfig, MemoryBackendConfig, SessionBackendConfig
from apx_agent import _memory_wiring as mw
from apx_agent._doctor import Status, check_lakebase_deprecated


@pytest.fixture(autouse=True)
def _fresh_once_cache() -> Any:
    mw._warn_lakebase_deprecated.cache_clear()
    yield
    mw._warn_lakebase_deprecated.cache_clear()


def _deprecations(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "deprecated (ADR 0001)" in r.getMessage()]


def _lakebase_session() -> SessionBackendConfig:
    return SessionBackendConfig(type="lakebase", host="h.example", database="db")


def test_memory_lakebase_warns(caplog: pytest.LogCaptureFixture) -> None:
    cfg = MemoryBackendConfig(type="lakebase", host="h.example", database="db")
    with caplog.at_level(logging.WARNING, logger=mw.__name__):
        assert mw._build_memory_store(cfg, ws=None) is None  # unchanged ws=None behaviour
    assert _deprecations(caplog) == [f"memory type='lakebase': {mw.LAKEBASE_DEPRECATION}"]


def test_session_lakebase_warns_and_still_builds(caplog: pytest.LogCaptureFixture) -> None:
    sentinel = object()
    with caplog.at_level(logging.WARNING, logger=mw.__name__), patch(
        "apx_agent._lakebase_engine.build_lakebase_engine", return_value=MagicMock()
    ), patch("apx_agent._conversation_lakebase.LakebaseConversationStore", return_value=sentinel):
        assert mw._build_conversation_store(_lakebase_session(), ws=MagicMock()) is sentinel
    assert _deprecations(caplog) == [f"session type='lakebase': {mw.LAKEBASE_DEPRECATION}"]


def test_checkpointer_lakebase_warns_once_and_still_builds(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apx_agent._checkpoint_lakebase as cp

    sentinel = object()
    monkeypatch.setattr(cp, "build_lakebase_checkpointer", lambda **_k: sentinel)
    config = AgentConfig(name="t", target="responses_agent", model="m", session=_lakebase_session())
    with caplog.at_level(logging.WARNING, logger=mw.__name__):
        assert mw.resolve_checkpointer(config, ws=MagicMock()) is sentinel
        assert mw.resolve_checkpointer(config, ws=MagicMock()) is sentinel
    assert _deprecations(caplog) == [f"checkpointer type='lakebase': {mw.LAKEBASE_DEPRECATION}"]


def test_inmemory_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=mw.__name__):
        mw._build_memory_store(MemoryBackendConfig(type="inmemory"), ws=None)
        mw._build_conversation_store(SessionBackendConfig(type="inmemory"), ws=None)
    assert _deprecations(caplog) == []


def _doctor(tmp_path: Any, cfg: AgentConfig) -> Any:
    with patch("apx_agent._doctor._is_apx_project", return_value=True), patch(
        "apx_agent._inspection._load_agent_config", return_value=cfg
    ):
        return check_lakebase_deprecated(tmp_path)


def test_doctor_warns_for_lakebase_memory_and_session(tmp_path: Any) -> None:
    cfg = AgentConfig(
        name="t", target="responses_agent", model="m", session=_lakebase_session(),
        memory=MemoryBackendConfig(type="lakebase", host="h.example", database="db"),
    )
    check = _doctor(tmp_path, cfg)
    assert check is not None
    assert check.status is Status.WARN
    assert check.detail == "memory and session type='lakebase' is deprecated"
    assert check.fix == mw.LAKEBASE_DEPRECATION


def test_doctor_silent_without_lakebase(tmp_path: Any) -> None:
    cfg = AgentConfig(name="t", model="m", memory=MemoryBackendConfig(type="managed", store_name="agent-memory"))
    assert _doctor(tmp_path, cfg) is None
