"""The disposable session probe must lose the worker before a tool result exists."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

PROBE = Path(__file__).resolve().parents[2] / "scripts/probes/apx_h0f/server.py"
spec = importlib.util.spec_from_file_location("h0f_probe", PROBE)
assert spec is not None and spec.loader is not None
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def _checkpoint(messages: list[Any]) -> Any:
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
    ws.workspace.download.side_effect = lambda path: io.BytesIO(files[path])
    return SimpleNamespace(ws=ws, saver=saver, files=files)


def _context(*, recovery: bool, attempt: int) -> Any:
    return SimpleNamespace(
        invocation_id="invocation", session_id="session", attempt=attempt, is_recovery=recovery,
    )


@pytest.fixture
def sink() -> Any:
    return _checkpoint([
        HumanMessage(content=probe.MARKER),
        AIMessage(content="", tool_calls=[{
            "name": "mark", "args": {"value": probe.MARKER}, "id": probe.TOOL_CALL_ID,
        }]),
    ])


def test_first_attempt_exits_before_return_and_recovery_continues(
    sink: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WorkerLost(BaseException):
        pass

    def exit_worker(code: int) -> None:
        assert code == 86
        raise WorkerLost

    monkeypatch.setattr(probe.os, "_exit", exit_worker)
    mark = probe.make_mark(sink.ws, sink.saver, "/diag")
    token = probe.CONTEXT.set(_context(recovery=False, attempt=1))
    try:
        with pytest.raises(WorkerLost):
            mark(probe.MARKER)
        boundary = json.loads(sink.files["/diag/boundary-1.json"])
        assert boundary["recovery"] is False
        assert boundary["invocation_id"] == "invocation"
        assert boundary["session_id"] == "session"
        assert boundary["checkpoint"]["message_types"] == ["human", "ai"]
        assert boundary["checkpoint"]["tool_results"] == []
        probe.CONTEXT.set(_context(recovery=True, attempt=2))
        continued = json.loads(mark(probe.MARKER))
        assert continued == {
            "attempt": 2,
            "invocation_id": "invocation",
            "marker": probe.MARKER,
            "recovery": True,
            "session_id": "session",
        }
        assert "/diag/attempt-3.json" not in sink.files
    finally:
        probe.CONTEXT.reset(token)


def test_wrong_marker_and_committed_result_do_not_exit(
    sink: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_worker = MagicMock()
    monkeypatch.setattr(probe.os, "_exit", exit_worker)
    mark = probe.make_mark(sink.ws, sink.saver, "/diag")
    with pytest.raises(ValueError, match="marker"):
        mark("other")
    assert sink.files == {}
    sink.saver.get_tuple.return_value.checkpoint["channel_values"]["messages"].append(
        ToolMessage(content="already committed", tool_call_id=probe.TOOL_CALL_ID),
    )
    token = probe.CONTEXT.set(_context(recovery=False, attempt=1))
    try:
        with pytest.raises(AssertionError, match="worker-loss boundary"):
            mark(probe.MARKER)
        exit_worker.assert_not_called()
    finally:
        probe.CONTEXT.reset(token)


def test_model_calls_the_marker_once_and_otherwise_answers() -> None:
    model = probe.ProbeModel()
    crash = model._generate([HumanMessage(content=f"please {probe.MARKER}")]).generations[0].message
    assert crash.tool_calls[0]["args"] == {"value": probe.MARKER}
    assert crash.tool_calls[0]["id"] == probe.TOOL_CALL_ID
    quiet = model._generate([HumanMessage(content="ping")]).generations[0].message
    assert quiet.content == "pong"
    assert quiet.tool_calls == []
    done = model._generate([ToolMessage(content="continued", tool_call_id=probe.TOOL_CALL_ID)])
    assert done.generations[0].message.content == "Continued continued"


def test_create_app_refuses_without_the_disposable_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APX_H0F_ENABLE_CRASH", "no")
    with pytest.raises(ValueError, match="disposable"):
        probe.create_app()


def test_committed_live_record_matches_the_probe_and_the_same_invocation() -> None:
    """Artifact integrity only: this does not rerun the deployed fault."""
    root = Path(__file__).resolve().parents[2]
    record = json.loads((root / "docs/evidence/apx-h0f-2026-11-20/evidence.json").read_text())
    assert hashlib.sha256((root / record["code"]["probe"]).read_bytes()).hexdigest() == (
        record["code"]["probe_sha256"]
    )
    invocation_id = record["request"]["id"]
    session_id = record["request"]["session_id"]
    assert record["accepted"]["status"] == 202
    assert record["accepted"]["body"]["id"] == invocation_id
    assert record["http_after_crash"]["status"] == 502
    assert record["recovered_http"]["status"] == 200
    assert record["recovered_http"]["body"]["id"] == invocation_id
    assert record["recovered_http"]["body"]["status"] == "completed"
    assert record["duplicate_submission"]["body"]["status"] == "completed"
    for boundary, recovery, attempt in (
        (record["boundary_1"], False, 1),
        (record["boundary_2"], True, 2),
    ):
        assert boundary["invocation_id"] == invocation_id
        assert boundary["session_id"] == session_id
        assert boundary["recovery"] is recovery
        assert boundary["attempt"] == attempt
        assert boundary["versions"] == record["code"]["installed_versions"]
    assert record["boundary_1"]["checkpoint"]["message_types"] == ["human", "ai"]
    assert record["boundary_1"]["checkpoint"]["tool_results"] == []
    assert record["boundary_1"]["checkpoint"]["checkpoint_id"] == (
        record["checkpoint_after_crash"]["checkpoint_id"]
    )
    assert record["boundary_2"]["checkpoint"]["checkpoint_id"] == (
        record["boundary_1"]["checkpoint"]["checkpoint_id"]
    )
    assert record["checkpoint_after_crash"]["message_types"] == ["human", "ai"]
    assert record["checkpoint_after_crash"]["pending_write_channels"] == []
    assert record["checkpoint_after_recovery"]["message_types"] == ["human", "ai", "tool", "ai"]
    assert record["checkpoint_after_recovery"]["metadata"]["databricks_agentkit.invocation_id"] == (
        invocation_id
    )
    human = record["checkpoint_after_crash"]["messages"][0]["id"]
    tool_call = record["checkpoint_after_crash"]["messages"][1]["id"]
    recovered_ids = [message["id"] for message in record["checkpoint_after_recovery"]["messages"]]
    assert recovered_ids[:2] == [human, tool_call]
    attempts = [item["data"] for item in record["events"]["records"] if item["event"] == "h0f.attempt"]
    assert [item["attempt"] for item in attempts] == [1, 2]
    assert [item["recovery"] for item in attempts] == [False, True]
    assert record["attempts"]["raw_tool_attempts"] == 2
    assert record["attempts"]["attempt_3_absent"] is True
    assert len(record["diag_listing_after_duplicate"]) == 4
    assert record["replacement"]["initial_deployment"]["deployment_id"] != (
        record["replacement"]["replacement_deployment"]["deployment_id"]
    )
    assert record["replacement"]["while_worker_dead"]["app_status"]["running_instances"] == 0
    assert record["left_running"]["deleted"] is False
