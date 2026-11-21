"""Trace probe: the kill marker exports the open span, then exits before a result."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

PROBE = Path(__file__).resolve().parents[2] / "scripts/probes/apx_pzd/server.py"
spec = importlib.util.spec_from_file_location("pzd_probe", PROBE)
assert spec is not None and spec.loader is not None
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def _sink(messages: list[Any]) -> Any:
    saved = SimpleNamespace(
        checkpoint={"id": "pre-tool", "channel_values": {"messages": messages}},
        metadata={"databricks_agentkit.invocation_id": "invocation"},
        pending_writes=[],
    )
    saver = MagicMock()
    saver.get_tuple.return_value = saved
    files: dict[str, bytes] = {}
    ws = MagicMock()

    def upload(path: str, content: bytes, **kwargs: Any) -> None:
        assert kwargs["overwrite"] is False
        files[path] = content

    ws.workspace.upload.side_effect = upload
    return SimpleNamespace(ws=ws, saver=saver, files=files)


def test_export_open_trace_ends_and_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    span = MagicMock()
    fake = ModuleType("mlflow")
    fake.get_active_trace_id = lambda: "trace-1"  # type: ignore[attr-defined]
    fake.get_current_active_span = lambda: span  # type: ignore[attr-defined]
    fake.flush_trace_async_logging = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    recorded = probe.export_open_trace()
    assert recorded == {"trace_id": "trace-1", "ended": True, "flushed": True}
    span.end.assert_called_once_with()
    fake.flush_trace_async_logging.assert_called_once_with()


def test_kill_exports_then_exits_and_recovery_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    class WorkerLost(BaseException):
        pass

    monkeypatch.setattr(probe.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(probe.os, "_exit", lambda code: (_ for _ in ()).throw(WorkerLost(code)))
    monkeypatch.setattr(
        probe, "export_open_trace", lambda: {"trace_id": "trace-1", "ended": True, "flushed": True},
    )
    fake = ModuleType("mlflow")
    fake.get_active_trace_id = lambda: "trace-1"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    sink = _sink([
        HumanMessage(content=probe.KILL),
        AIMessage(content="", tool_calls=[{
            "name": "mark", "args": {"value": probe.KILL}, "id": probe.TOOL_CALL_ID,
        }]),
    ])
    mark = probe.make_mark(sink.ws, sink.saver, "/diag")
    token = probe.CONTEXT.set(SimpleNamespace(
        invocation_id="invocation", session_id="session", attempt=1, is_recovery=False,
    ))
    try:
        with pytest.raises(WorkerLost):
            mark(probe.KILL)
        exported = json.loads(sink.files["/diag/export-1.json"])
        boundary = json.loads(sink.files["/diag/boundary-1.json"])
        assert exported["export"]["trace_id"] == "trace-1"
        assert boundary["checkpoint"]["tool_results"] == []
        recovered = probe.CONTEXT.set(SimpleNamespace(
            invocation_id="invocation", session_id="session", attempt=2, is_recovery=True,
        ))
        try:
            body = json.loads(mark(probe.KILL))
        finally:
            probe.CONTEXT.reset(recovered)
        assert body["recovery"] is True
        assert body["continued"] is True
    finally:
        probe.CONTEXT.reset(token)


def test_create_app_refuses_without_the_disposable_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APX_PZD_ENABLE_CRASH", "no")
    with pytest.raises(ValueError, match="disposable"):
        probe.create_app()


def test_committed_live_record_matches_the_probe_and_the_traces() -> None:
    """Artifact integrity only: this does not rerun the deployed fault."""
    root = Path(__file__).resolve().parents[2]
    record = json.loads((root / "docs/evidence/apx-pzd-2026-11-21/evidence.json").read_text())
    assert hashlib.sha256((root / record["code"]["probe"]).read_bytes()).hexdigest() == (
        record["code"]["probe_sha256"]
    )
    assert record["commands"]["apps_stop_or_start"] is False
    assert record["symbols_inspected"]["native_handler_continues_traceparent"] is False
    loss = record["worker_loss"]
    killed = loss["request"]["id"]
    assert loss["accepted"]["status"] == 202
    assert loss["http_while_active_last"]["http"] == 200
    assert loss["http_while_active_last"]["invocation_status"] == "active"
    assert loss["http_after_crash"]["http"] == 502
    assert loss["app_while_dead"]["app_state"] == "CRASHED"
    assert loss["app_while_dead"]["running_instances"] == 0
    assert loss["app_while_dead"]["active_deployment_id"] == record["replacement"]["initial_deployment_id"]
    assert loss["boundary_1"]["message_types"] == ["human", "ai"]
    assert loss["boundary_1"]["tool_results"] == []
    assert loss["export_1"]["flushed"] is True
    replacement = record["replacement"]
    assert replacement["initial_deployment_id"] != replacement["replacement_deployment_id"]
    assert replacement["attempt_2"]["invocation_id"] == killed
    assert replacement["attempt_2"]["recovery"] is True
    assert replacement["boundary_2"]["checkpoint_id"] == loss["boundary_1"]["checkpoint_id"]
    assert replacement["final"]["status"] == 200
    assert replacement["final"]["invocation_id"] == killed
    assert replacement["final"]["invocation_status"] == "completed"
    assert replacement["checkpoint_after_recovery"]["metadata_invocation_id"] == killed
    assert "attempt-3.json" not in replacement["diag_listing"]
    traces = record["traces"]
    assert traces["filter_count"] == 2
    assert traces["initial"]["apx_tags"]["apx.invocation.id"] == killed
    assert traces["recovered"]["apx_tags"]["apx.invocation.id"] == killed
    assert traces["initial"]["apx_tags"]["apx.recovery"] == "false"
    assert traces["recovered"]["apx_tags"]["apx.recovery"] == "true"
    assert traces["initial"]["trace_id"] == loss["attempt_1"]["active_trace_id"]
    assert traces["recovered"]["trace_id"] == replacement["attempt_2"]["active_trace_id"]
    assert traces["inbound_equals_any_exported"] is False
    assert traces["apx_traceparent_tag_present"] is False
    assert loss["inbound_trace_id_hex"] not in traces["initial"]["trace_id"]
    assert loss["inbound_trace_id_hex"] not in traces["recovered"]["trace_id"]
    assert "missing span data" in traces["get_trace"]["initial"]
    assert "missing span data" in traces["get_trace"]["recovered"]
    assert record["final_ping"]["http_post"] == 202
    assert record["final_ping"]["http_get"] == 200
    assert record["final_ping"]["output_content"] == "pong"
    assert record["left_running"]["deleted"] is False
    assert record["left_running"]["app_state"] == "RUNNING"
    assert record["left_running"]["running_instances"] == 1
