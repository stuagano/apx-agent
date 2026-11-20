"""The disposable live probe must fail closed and exercise the uncertain write window."""
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
from databricks.sdk.errors import ResourceAlreadyExists
from langchain_core.messages import AIMessage, ToolMessage

PROBE = Path(__file__).resolve().parents[2] / "scripts/probes/apx_aub/server.py"
spec = importlib.util.spec_from_file_location("aub_probe", PROBE)
assert spec is not None and spec.loader is not None
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture
def sink() -> Any:
    files: dict[str, bytes] = {}
    ws = MagicMock()

    def upload(path: str, content: bytes, **kwargs: Any) -> None:
        assert kwargs["overwrite"] is False
        if path in files:
            raise ResourceAlreadyExists("create-only duplicate")
        files[path] = content

    ws.workspace.upload.side_effect = upload
    ws.workspace.download.side_effect = lambda path: io.BytesIO(files[path])
    saved = SimpleNamespace(checkpoint={"id": "pre-tool", "channel_values": {
        "messages": [AIMessage(content="", tool_calls=[{
            "name": "record", "args": {"value": "key"}, "id": "aub-call",
        }])],
    }}, metadata={"databricks_agentkit.invocation_id": "invocation"}, pending_writes=[])
    saver = MagicMock()
    saver.get_tuple.return_value = saved
    return SimpleNamespace(ws=ws, saver=saver, files=files)


def test_write_then_exit_before_return_and_recovery_deduplicates(
    sink: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WorkerLost(BaseException):
        pass

    def exit_worker(code: int) -> None:
        assert code == 86
        raise WorkerLost

    monkeypatch.setattr(probe.os, "_exit", exit_worker)
    record = probe.make_record(sink.ws, sink.saver, "/sink", "key")
    token = probe.CONTEXT.set(SimpleNamespace(invocation_id="invocation", session_id="session",
                                             attempt=1, is_recovery=False))
    try:
        with pytest.raises(WorkerLost):
            record("key")
        before = json.loads(sink.files["/sink/boundary-1.json"])
        assert before["created"] is True
        assert before["checkpoint"]["tool_results"] == []
        assert before["sink_readback"] == {"effect_key": "key", "value": "harmless-test-mutation"}
        original = sink.files["/sink/effect.json"]
        probe.CONTEXT.set(SimpleNamespace(invocation_id="invocation", session_id="session",
                                          attempt=2, is_recovery=True))
        assert json.loads(record("key")) == {"effect_key": "key", "created": False}
        assert sink.files["/sink/effect.json"] == original
        assert len([p for p in sink.files if "/attempt-" in p]) == 2
        assert len([p for p in sink.files if p.endswith("/effect.json")]) == 1
        assert json.loads(sink.files["/sink/boundary-2.json"])["created"] is False
    finally:
        probe.CONTEXT.reset(token)


def test_conflicting_business_payload_never_overwrites_or_reports_success(sink: Any) -> None:
    sink.files["/sink/effect.json"] = b'{"value": "other"}'
    token = probe.CONTEXT.set(SimpleNamespace(invocation_id="invocation", session_id="session",
                                             attempt=2, is_recovery=True))
    try:
        with pytest.raises(ValueError, match="reconciliation required"):
            probe.make_record(sink.ws, sink.saver, "/sink", "key")("key")
        assert sink.files["/sink/effect.json"] == b'{"value": "other"}'
        assert "/sink/boundary-2.json" not in sink.files
    finally:
        probe.CONTEXT.reset(token)


def test_wrong_key_and_already_checkpointed_result_refuse_fault_injection(
    sink: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_worker = MagicMock()
    monkeypatch.setattr(probe.os, "_exit", exit_worker)
    record = probe.make_record(sink.ws, sink.saver, "/sink", "key")
    with pytest.raises(ValueError, match="business key"):
        record("wrong")
    assert sink.files == {}
    sink.saver.get_tuple.return_value.checkpoint["channel_values"]["messages"].append(
        ToolMessage(content="already committed", tool_call_id="aub-call"),
    )
    token = probe.CONTEXT.set(SimpleNamespace(invocation_id="invocation", session_id="session",
                                             attempt=1, is_recovery=False))
    try:
        with pytest.raises(AssertionError, match="crash boundary"):
            record("key")
        exit_worker.assert_not_called()
    finally:
        probe.CONTEXT.reset(token)


def test_probe_model_calls_only_the_fixed_business_key_then_finishes() -> None:
    model = probe.ProbeModel(effect_key="key")
    first = model._generate([AIMessage(content="start")]).generations[0].message
    assert first.tool_calls[0]["args"] == {"value": "key"}
    final = model._generate([ToolMessage(content="deduplicated", tool_call_id="aub-call")])
    assert final.generations[0].message.content == "Recorded deduplicated"


def test_committed_live_record_matches_deployed_probe_and_boundaries() -> None:
    """Artifact integrity only: this does not rerun or simulate a deployed proof."""
    root = Path(__file__).resolve().parents[2]
    record = json.loads(
        (root / "docs/evidence/apx-aub-2026-11-20/evidence.json").read_text()
    )
    assert hashlib.sha256((root / record["code"]["probe"]).read_bytes()).hexdigest() == (
        record["code"]["probe_sha256"]
    )
    invocation_id = record["request"]["id"]
    session_id = record["request"]["session_id"]
    key = record["fault_boundary"]["idempotency_key"]
    files = record["sink_files"]
    for number, recovery in ((1, False), (2, True)):
        attempt = files[f"attempt-{number}.json"]
        boundary = files[f"boundary-{number}.json"]
        for value in (attempt, boundary):
            assert value["invocation_id"] == invocation_id
            assert value["session_id"] == session_id
            assert value["effect_key"] == key
            assert value["attempt"] == number
            assert value["recovery"] is recovery
        assert boundary["created"] is (not recovery)
        assert boundary["checkpoint"]["tool_results"] == []
        assert boundary["checkpoint"]["pending_write_channels"] == []
    assert files["attempt-1.json"]["versions"] == files["attempt-2.json"]["versions"]
    assert record["sink_after_crash"] == files["effect.json"]
    assert [m["type"] for m in record["checkpoint_after_crash"]["messages"]] == ["human", "ai"]
    assert record["checkpoint_after_crash"]["pending_writes"] == []
    assert [m["type"] for m in record["checkpoint_after_recovery"]["messages"]] == [
        "human", "ai", "tool", "ai",
    ]
    assert record["checkpoint_after_recovery"]["pending_write_channels"] == []
    assert record["raw_tool_attempts"] == 2
    assert record["effective_mutations"] == 1
    assert len(record["sink_listing"]) == len(record["sink_listing_after_duplicate"]) == 5
    assert record["recovered_http"]["body"]["id"] == invocation_id
    assert record["recovered_http"]["body"]["status"] == "completed"
    assert record["duplicate_submission"]["body"]["status"] == "completed"
    assert all(check["result"] == "absent" for check in record["teardown"]["checks"].values())
