"""Disconnect probe: a hold returns, and only the kill marker exits before a result."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from apx_agent._cancellation import CancellationRegistry

PROBE = Path(__file__).resolve().parents[2] / "scripts/probes/apx_4ag/server.py"
spec = importlib.util.spec_from_file_location("ag4_probe", PROBE)
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
        if path in files:
            raise AssertionError(f"refusing to overwrite {path}")
        files[path] = content

    ws.workspace.upload.side_effect = upload
    return SimpleNamespace(ws=ws, saver=saver, files=files)


def _context(*, recovery: bool, attempt: int) -> Any:
    return SimpleNamespace(
        invocation_id="invocation", session_id="session", attempt=attempt, is_recovery=recovery,
    )


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("APX_4AG_HOLD_SECONDS", "12")
    monkeypatch.setenv("APX_4AG_KILL_DELAY_SECONDS", "4")
    return _sink([
        HumanMessage(content=probe.HOLD),
        AIMessage(content="", tool_calls=[{
            "name": "pace", "args": {"value": probe.HOLD}, "id": probe.TOOL_CALL_ID,
        }]),
    ])


def test_hold_returns_and_kill_exits_only_before_a_tool_result(
    sink: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WorkerLost(BaseException):
        pass

    slept: list[int] = []
    monkeypatch.setattr(probe.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr(probe.os, "_exit", lambda code: (_ for _ in ()).throw(WorkerLost(code)))
    pace = probe.make_pace(sink.ws, sink.saver, "/diag")
    token = probe.CONTEXT.set(_context(recovery=False, attempt=1))
    try:
        held = json.loads(pace(probe.HOLD))
        assert held["continued"] is True
        assert held["recovery"] is False
        assert slept == [12]
        with pytest.raises(WorkerLost):
            pace(probe.KILL)
        assert slept == [12, 4]
        boundary = json.loads(sink.files["/diag/boundary-1-4ag-worker-loss.json"])
        assert boundary["checkpoint"]["tool_results"] == []
        assert boundary["checkpoint"]["message_types"] == ["human", "ai"]
    finally:
        probe.CONTEXT.reset(token)


def test_committed_tool_result_refuses_the_kill(sink: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    exit_worker = MagicMock()
    monkeypatch.setattr(probe.os, "_exit", exit_worker)
    monkeypatch.setattr(probe.time, "sleep", lambda seconds: None)
    sink.saver.get_tuple.return_value.checkpoint["channel_values"]["messages"].append(
        ToolMessage(content="already committed", tool_call_id=probe.TOOL_CALL_ID),
    )
    token = probe.CONTEXT.set(_context(recovery=False, attempt=1))
    try:
        with pytest.raises(AssertionError, match="worker-loss boundary"):
            probe.make_pace(sink.ws, sink.saver, "/diag")(probe.KILL)
        exit_worker.assert_not_called()
    finally:
        probe.CONTEXT.reset(token)


def test_model_routes_hold_kill_and_quiet_turns() -> None:
    model = probe.ProbeModel()
    hold = model._generate([HumanMessage(content=probe.HOLD)]).generations[0].message
    assert hold.tool_calls[0]["args"] == {"value": probe.HOLD}
    kill = model._generate([HumanMessage(content=probe.KILL)]).generations[0].message
    assert kill.tool_calls[0]["args"] == {"value": probe.KILL}
    quiet = model._generate([HumanMessage(content="ping")]).generations[0].message
    assert quiet.content == "pong"
    assert quiet.tool_calls == []


def test_create_app_refuses_without_the_disposable_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APX_4AG_ENABLE_CRASH", "no")
    with pytest.raises(ValueError, match="disposable"):
        probe.create_app()


def test_legacy_cancellation_registry_remains() -> None:
    """The in-process kill switch stays; this probe does not replace it."""
    registry = CancellationRegistry()
    assert registry.cancel_all("not-a-live-cancel") == 0
    assert registry.active_count == 0


def test_committed_live_record_matches_the_probe_and_the_cursors() -> None:
    """Artifact integrity only: this does not rerun the deployed fault."""
    root = Path(__file__).resolve().parents[2]
    record = json.loads((root / "docs/evidence/apx-4ag-2026-11-21/evidence.json").read_text())
    assert hashlib.sha256((root / record["code"]["probe"]).read_bytes()).hexdigest() == (
        record["code"]["probe_sha256"]
    )
    assert record["cancellation"]["supported"] is False
    assert record["commands"]["apps_stop_or_start"] is False
    assert record["left_running"]["deleted"] is False
    hold = record["hold"]
    invocation_id = hold["request"]["id"]
    assert hold["accepted"]["status"] == 202
    assert hold["accepted"]["body"]["id"] == invocation_id
    assert hold["status_at_disconnect"]["status"] == 200
    assert hold["status_at_disconnect"]["invocation_status"] == "active"
    assert hold["sse_disconnect"]["cursor"] == "22"
    assert hold["sse_disconnect"]["cursor"] not in hold["replay_after_cursor"]["event_ids"]
    assert hold["replay_after_cursor"]["event_ids"] == ["23", "24", "25", "26"]
    assert hold["replay_after_cursor"]["event_names"][-1] == "run.completed"
    assert hold["final"]["invocation_status"] == "completed"
    assert hold["final"]["invocation_id"] == invocation_id
    assert hold["cancel_while_active"]["post_cancel"] == 404
    assert hold["cancel_while_active"]["delete_invocation"] == 405
    assert hold["cancel_while_active"]["readback"]["invocation_status"] == "active"
    assert hold["cancel_after_completed"]["readback"]["invocation_status"] == "completed"
    assert hold["boundary"]["message_types"] == ["human", "ai"]
    assert hold["boundary"]["tool_results"] == []
    assert hold["checkpoint_after_completion"]["message_types"] == ["human", "ai", "tool", "ai"]
    loss = record["worker_loss"]
    killed = loss["request"]["id"]
    assert loss["accepted"]["status"] == 202
    assert loss["http_after_disconnect"][-1]["http"] == 502
    assert loss["http_after_disconnect"][0]["invocation_status"] == "active"
    assert loss["app_while_dead"]["app_state"] == "CRASHED"
    assert loss["app_while_dead"]["running_instances"] == 0
    assert loss["app_while_dead"]["compute_state"] == "ACTIVE"
    assert loss["boundary_1"]["checkpoint_id"] == loss["checkpoint_after_crash"]["checkpoint_id"]
    assert loss["boundary_1"]["message_types"] == ["human", "ai"]
    assert loss["boundary_1"]["tool_results"] == []
    replacement = record["replacement"]
    assert replacement["initial_deployment_id"] != replacement["replacement_deployment_id"]
    assert replacement["attempt_2"]["invocation_id"] == killed
    assert replacement["attempt_2"]["recovery"] is True
    assert replacement["attempt_1_still_present"]["recovery"] is False
    assert replacement["boundary_2"]["checkpoint_id"] == loss["boundary_1"]["checkpoint_id"]
    assert replacement["final"]["status"] == 200
    assert replacement["final"]["invocation_id"] == killed
    assert replacement["final"]["invocation_status"] == "completed"
    assert loss["sse_disconnect"]["cursor"] == "28"
    assert "28" not in replacement["replay_after_cursor"]["event_ids"]
    attempts = [
        item["data"]
        for item in replacement["replay_full"]["records"]
        if item["event"] == "4ag.attempt"
    ]
    assert [item["attempt"] for item in attempts] == [1, 2]
    assert [item["recovery"] for item in attempts] == [False, True]
    assert replacement["checkpoint_after_recovery"]["message_types"] == ["human", "ai", "tool", "ai"]
    assert replacement["checkpoint_after_recovery"]["metadata_invocation_id"] == killed
    assert "attempt-3" not in " ".join(replacement["diag_listing"])
    assert loss["attempt_1"]["versions"] == record["code"]["installed_versions"]
    assert record["final_ping"]["http_post"] == 202
    assert record["final_ping"]["http_get"] == 200
    assert record["final_ping"]["output_content"] == "pong"
    assert record["left_running"]["app_state"] == "RUNNING"
    assert record["left_running"]["running_instances"] == 1
