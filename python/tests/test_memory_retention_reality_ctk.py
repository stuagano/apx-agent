"""Claim-vs-reality: declared memory retention deletes expired rows (#1090).

The claim: ``memory.retention`` declares days per namespace (plus an optional
default), Lakebase enforces it with a delete pass at app start, ``doctor``
reports it, and the managed backend reports that the platform states no
retention instead of inventing one. In-memory rejects the setting.

The delete pass is plain SQL, so it runs against a real SQLite table with the
Lakebase column set, and every check reads the rows back through the store.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sa = pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from apx_agent import AgentConfig, LlmAgent, MemoryBackendConfig, create_app  # noqa: E402
from apx_agent._doctor import Status, check_memory_retention  # noqa: E402
from apx_agent._memory import MemoryFilter  # noqa: E402
from apx_agent._memory_lakebase import LakebaseMemoryStore  # noqa: E402
from apx_agent._memory_wiring import purge_declared_memory  # noqa: E402
from apx_agent._models import MemoryRetention  # noqa: E402
from tests.test_runtime_targets import execution  # noqa: E402,F401

NOW = time.time()
DAY = 86400.0
ROWS = {
    # id: (namespace, age in days)
    "scratch-old": ("scratch", 10),
    "scratch-fresh": ("scratch", 1),
    "default-old": ("default", 100),
    "default-fresh": ("default", 30),
    "facts-old": ("facts", 100),
}


def _store(url: str = "sqlite://") -> LakebaseMemoryStore:
    engine = sa.create_engine(url, poolclass=sa.pool.StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE apx_memories (id TEXT PRIMARY KEY, principal_id TEXT, namespace TEXT, "
            "content TEXT, tags TEXT, importance REAL, embedding TEXT, metadata TEXT, "
            "created_at REAL, updated_at REAL)"
        ))
        for mid, (ns, age) in ROWS.items():
            conn.execute(
                sa.text("INSERT INTO apx_memories VALUES (:id, 'u', :ns, :id, NULL, 0.5, NULL, '{}', :t, :t)"),
                {"id": mid, "ns": ns, "t": NOW - age * DAY},
            )
    return LakebaseMemoryStore(
        engine=engine, embedding_fn=lambda texts: [[1.0] for _ in texts], embedding_dim=1, auto_create=False,
    )


def _ids(store: LakebaseMemoryStore) -> set[str]:
    return {m.id for m in store.list(MemoryFilter(principal_id="u"))}


@pytest.mark.unit
def test_purge_deletes_expired_rows_and_keeps_fresh_ones() -> None:
    store = _store()
    deleted = store.purge_expired(MemoryRetention(default_days=90, namespaces={"scratch": 7}), now=NOW)
    assert deleted == 3
    assert _ids(store) == {"scratch-fresh", "default-fresh"}


@pytest.mark.unit
def test_without_default_unlisted_namespaces_are_kept() -> None:
    store = _store()
    assert store.purge_expired(MemoryRetention(namespaces={"scratch": 7}), now=NOW) == 1
    assert _ids(store) == set(ROWS) - {"scratch-old"}


@pytest.mark.unit
def test_app_start_runs_the_delete_pass() -> None:
    store = _store()
    agent = LlmAgent()
    agent._apx_memory_store = store
    agent._apx_memory_config = MemoryBackendConfig(type="lakebase", retention={"default_days": 1})
    with patch("apx_agent._wiring._make_workspace_client", return_value=MagicMock()):
        app = create_app(agent, config=AgentConfig(name="t", model="m"))
        with TestClient(app):
            pass
    # Every seeded row is at least a day old against the real clock.
    assert _ids(store) == set()


@pytest.mark.unit
def test_durable_server_start_runs_the_delete_pass(execution, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # noqa: F811
    """The Apps API deploy path (durable_agent_server) purges at startup too (#1101)."""
    from langgraph.checkpoint.memory import InMemorySaver

    from apx_agent._durable_agent import compile_to_durable_agent_server

    (tmp_path / "pyproject.toml").write_text('[tool.apx.deploy]\nname = "proof"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    store = _store()
    agent = execution.agent
    agent._apx_memory_store = store
    agent._apx_memory_config = MemoryBackendConfig(type="lakebase", retention={"namespaces": {"scratch": 7}})
    app = compile_to_durable_agent_server(agent, model="test", service_ws=execution.ws, checkpointer=InMemorySaver())
    assert _ids(store) == set(ROWS)  # compiling never deletes; only the server start does
    with TestClient(app):
        pass
    assert _ids(store) == set(ROWS) - {"scratch-old"}


@pytest.mark.unit
def test_memory_purge_cli_deletes_expired_rows_and_reports_count(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from click.testing import CliRunner

    from apx_agent.cli import main

    store = _store()
    cfg = AgentConfig(name="t", model="m", memory=MemoryBackendConfig(
        type="lakebase", host="h", database="d", embedding_model="e", embedding_dim=1,
        retention={"default_days": 90, "namespaces": {"scratch": 7}},
    ))
    monkeypatch.setattr("apx_agent.cli._effective_config", lambda cwd: cfg)
    monkeypatch.setattr("apx_agent._memory_wiring._build_memory_store", lambda mcfg, ws: store)
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", MagicMock())
    result = CliRunner().invoke(main, ["memory", "purge"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"deleted": 3, "table": "apx_memories"}
    assert _ids(store) == {"scratch-fresh", "default-fresh"}


@pytest.mark.unit
def test_memory_purge_cli_without_declared_retention_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from apx_agent.cli import main

    monkeypatch.setattr("apx_agent.cli._effective_config",
                        lambda cwd: AgentConfig(name="t", model="m", memory=MemoryBackendConfig(type="inmemory")))
    result = CliRunner().invoke(main, ["memory", "purge"])
    assert result.exit_code != 0
    assert "No memory retention declared" in result.output


@pytest.mark.unit
def test_doctor_durable_startup_never_deletes_rows(tmp_path) -> None:
    """doctor --durable enters the durable lifespan offline; it must not run the delete pass."""
    from apx_agent._doctor_durable import check_durable_readiness

    db = tmp_path / "memories.db"
    store = _store(f"sqlite:///{db}")
    project = tmp_path / "keep"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "keep"\nversion = "0.1.0"\n\n[tool.apx.deploy]\nname = "keep"\n'
    )
    (project / "agent.py").write_text(
        "import sqlalchemy as sa\n"
        "from apx_agent import LlmAgent, MemoryBackendConfig\n"
        "from apx_agent._memory_lakebase import LakebaseMemoryStore\n"
        "agent = LlmAgent(name='keep', tools=[])\n"
        f"agent._apx_memory_store = LakebaseMemoryStore(engine=sa.create_engine({f'sqlite:///{db}'!r}), "
        "embedding_fn=lambda t: [[1.0] for _ in t], embedding_dim=1, auto_create=False)\n"
        "agent._apx_memory_config = MemoryBackendConfig(type='lakebase', retention={'default_days': 1})\n"
        "agent._apx_memory_attached = True\n"
    )
    checks = {c.name: c for c in check_durable_readiness(project)}
    assert checks["Startup"].status is Status.OK, checks["Startup"].detail
    assert _ids(store) == set(ROWS)


@pytest.mark.unit
def test_failed_delete_pass_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    broken = MagicMock(table_name="apx_memories")
    broken.purge_expired.side_effect = RuntimeError("connection refused")
    agent = MagicMock(_apx_memory_store=broken,
                      _apx_memory_config=MemoryBackendConfig(type="lakebase", retention={"default_days": 1}))
    assert purge_declared_memory(agent) is None
    assert "connection refused" in caplog.text


@pytest.mark.unit
@pytest.mark.parametrize("store_type", ["inmemory", "managed"])
def test_retention_rejected_where_it_cannot_be_enforced(store_type: str) -> None:
    with pytest.raises(ValidationError, match="memory.retention requires type='lakebase'"):
        MemoryBackendConfig(type=store_type, store_name="agent-memory", retention={"default_days": 30})


@pytest.mark.unit
def test_empty_retention_rejected() -> None:
    with pytest.raises(ValidationError, match="default_days or at least one namespace"):
        MemoryRetention()


@contextmanager
def _doctor_sees(memory: MemoryBackendConfig) -> Any:
    cfg = AgentConfig(name="t", target="responses_agent", model="m", memory=memory)
    with patch("apx_agent._doctor._is_apx_project", return_value=True), patch(
        "apx_agent._inspection._load_agent_config", return_value=cfg
    ):
        yield


@pytest.mark.unit
def test_doctor_reports_declared_lakebase_retention(tmp_path) -> None:
    mem = MemoryBackendConfig(type="lakebase", retention={"default_days": 90, "namespaces": {"scratch": 7}})
    with _doctor_sees(mem):
        check = check_memory_retention(tmp_path)
    assert check is not None and check.status is Status.OK
    assert check.detail == "scratch=7d, default=90d (delete pass runs at app start)"


@pytest.mark.unit
def test_doctor_reports_managed_platform_states_no_retention(tmp_path) -> None:
    with _doctor_sees(MemoryBackendConfig(type="managed", store_name="agent-memory")):
        check = check_memory_retention(tmp_path)
    assert check is not None
    assert "platform states no retention" in check.detail
