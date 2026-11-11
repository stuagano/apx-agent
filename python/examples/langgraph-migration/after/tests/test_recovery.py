"""Offline checks of the runtime adapter: no Databricks, no model, no Session Store."""

import asyncio
import types

from langgraph.checkpoint.memory import InMemorySaver

from databricks_agentkit.langgraph import session_store

session_store._saver = InMemorySaver()  # pin before the adapter builds its graph

from rca import graph as rca_graph  # noqa: E402
from runtime import adapter  # noqa: E402


class FakeLLM:
    calls = 0

    def __init__(self, **_):
        pass

    def invoke(self, _messages):
        FakeLLM.calls += 1
        return types.SimpleNamespace(content="root cause: stale forecast inputs")


class WorkerDied(Exception):
    pass


def _context(invocation_id: str, events: list):
    async def emit(event):
        events.append(event)
        return len(events)

    return types.SimpleNamespace(invocation_id=invocation_id, session_id=invocation_id, emit=emit)


def _steps(events: list) -> list:
    return [e["nodes"] for e in events if e["type"] == "step"]


def test_recover_resumes_from_checkpoint_without_rerunning_earlier_steps(monkeypatch):
    monkeypatch.setattr(rca_graph, "ChatDatabricks", FakeLLM)
    FakeLLM.calls = 0
    first, second = [], []

    def die():
        raise WorkerDied

    monkeypatch.setattr(rca_graph, "BEFORE_ANALYZE", die)
    try:
        asyncio.run(adapter.invoke({"tenant": "acme"}, _context("inv-crash", first)))
    except WorkerDied:
        pass
    assert _steps(first) == [["compute_kpis"], ["detect"]]

    monkeypatch.setattr(rca_graph, "BEFORE_ANALYZE", lambda: None)
    out = asyncio.run(adapter.recover({"tenant": "acme"}, _context("inv-crash", second)))

    assert out["resumed_from_checkpoint"] is True
    assert _steps(second) == [["analyze"]]  # KPI + detect steps are not replayed
    assert out["anomalies"] == ["forecast_accuracy"]
    assert out["report"] == "root cause: stale forecast inputs"
    assert FakeLLM.calls == 1


def test_crash_flag_is_ignored_unless_enabled(monkeypatch):
    monkeypatch.setattr(rca_graph, "ChatDatabricks", FakeLLM)
    monkeypatch.setattr(adapter, "CRASH_TEST_ENABLED", False)
    events = []
    out = asyncio.run(
        adapter.invoke({"tenant": "acme", "crash_test": True}, _context("inv-safe", events))
    )
    assert out["report"] == "root cause: stale forecast inputs"


def test_healthy_tenant_skips_the_llm(monkeypatch):
    monkeypatch.setattr(rca_graph, "ChatDatabricks", FakeLLM)
    FakeLLM.calls = 0
    rca_graph.KPIS["healthy"] = {"forecast_accuracy": 0.95, "fill_rate": 0.99, "inventory_turns": 9.0}
    out = asyncio.run(adapter.invoke({"tenant": "healthy"}, _context("inv-healthy", [])))
    assert out == {"anomalies": [], "report": None}
    assert FakeLLM.calls == 0
