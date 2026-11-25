"""genie_agent_tool (#1256): Genie Agent mode over SSE, progress, cited report, errors.

The Agent mode responses endpoint is called through ``ws.api_client.do(raw=True)``
and mocked here with an in-memory SSE byte stream.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors import NotFound, PermissionDenied

import apx_agent.genie as g
from apx_agent import end_genie_session, genie_agent_tool
from apx_agent._errors import ToolError
from apx_agent._mlflow_tracing import _ACTIVE_SESSION


def _sse(events: list[dict[str, Any]]) -> bytes:
    return b"".join(
        f"event: {e['type']}\r\ndata: {json.dumps(e)}\r\n\r\n".encode() for e in events
    )


class _Stream:
    """A raw SDK stream over fixed bytes, read in the caller's block size."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.pos = 0
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        chunk = self.payload[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


def _response(conversation_id: str, status: str, output: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"object": "response", "id": f"r-{conversation_id}", "model": "genie-agent", "status": status,
            "conversation_id": conversation_id, "output": output, **extra}


REASONING = {"type": "reasoning", "id": "rs1", "status": "completed",
             "content": [{"type": "reasoning_text", "text": "I need revenue by region."}], "summary": []}
CALL = {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "execute_sql",
        "arguments": json.dumps({"sql": "SELECT region, SUM(rev) FROM sales GROUP BY region"})}
CALL_OUT = {"type": "function_call_output", "id": "call1_output", "status": "completed",
            "output": "Revenue by region\n| region | rev |\n|---|---|\n| west | 10 |"}
TABLE = {"type": "table", "columns": ["region", "rev"], "total_row_count": 1}
REPORT = {"type": "message", "role": "assistant", "id": "m1", "content": [
    {"type": "output_text", "text": "West leads revenue.",
     "annotations": [{"type": "url_citation", "title": "sales", "url": "https://x/sales"}]},
    TABLE,
]}


def _events(conversation_id: str = "c1", final: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    output = [REASONING, CALL, CALL_OUT, REPORT]
    return [
        {"type": "response.created", "sequence_number": 0,
         "response": _response(conversation_id, "in_progress", [])},
        {"type": "response.output_item.added", "sequence_number": 1, "item": {**REASONING, "status": "in_progress"}},
        {"type": "response.output_item.done", "sequence_number": 2, "item": REASONING},
        {"type": "response.output_item.done", "sequence_number": 3, "item": CALL},
        {"type": "response.output_item.done", "sequence_number": 4, "item": CALL_OUT},
        {"type": "response.output_item.done", "sequence_number": 5, "item": REPORT},
        final or {"type": "response.completed", "sequence_number": 6,
                  "response": _response(conversation_id, "completed", output)},
    ]


def _ws(*conversation_ids: str) -> MagicMock:
    """A client whose responses endpoint streams one full turn per conversation id."""
    ws = MagicMock()
    streams = [_Stream(_sse(_events(c))) for c in conversation_ids]
    ws.api_client.do.side_effect = [{"contents": s} for s in streams]
    ws.streams = streams
    return ws


def _body(ws: MagicMock, call: int) -> dict[str, Any]:
    return ws.api_client.do.call_args_list[call].kwargs["body"]


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    registry = g.GenieConversations()
    monkeypatch.setattr(g, "_AGENT_CONVERSATIONS", registry)
    token = _ACTIVE_SESSION.set("")
    yield registry
    _ACTIVE_SESSION.reset(token)


@pytest.fixture
def progress(monkeypatch):
    events: list[dict[str, Any]] = []

    def _record(message: str, **attributes: Any) -> None:
        events.append({"message": message, **attributes})

    monkeypatch.setattr(g, "emit_progress", _record)
    return events


@pytest.mark.asyncio
async def test_streams_reasoning_and_sql_as_progress_before_the_stream_ends(monkeypatch):
    ws = _ws("c1")
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(g, "emit_progress", lambda message, **a: seen.append(
        (a["kind"], ws.streams[0].pos < len(ws.streams[0].payload))))

    await genie_agent_tool("space-1")(question="revenue by region?", ws=ws)

    # One event per finished reasoning step and SQL call, each while the stream was still open.
    assert seen == [("reasoning", True), ("sql", True)]


@pytest.mark.asyncio
async def test_progress_carries_the_reasoning_text_and_sql(progress):
    await genie_agent_tool("space-1")(question="revenue by region?", ws=_ws("c1"))

    assert progress == [
        {"message": "Genie reasoning: I need revenue by region.", "kind": "reasoning"},
        {"message": "Genie is running SQL", "kind": "sql",
         "sql": "SELECT region, SUM(rev) FROM sales GROUP BY region"},
    ]


@pytest.mark.asyncio
async def test_progress_lands_on_the_real_tool_span_across_the_worker_thread(tmp_path):
    """Read-back: the streamed steps are apx.progress events on the caller's MLflow span."""
    import mlflow

    old_uri = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f"file://{tmp_path}")
    try:
        mlflow.set_experiment("apx-genie-agent-progress")
        mlflow.tracing.enable()
        with mlflow.start_span(name="ask_genie_agent") as span:
            await genie_agent_tool("space-1")(question="q", ws=_ws("c1"))
    finally:
        mlflow.set_tracking_uri(old_uri)

    progress = [(e.attributes or {}) for e in span.events or [] if e.name == "apx.progress"]
    assert [p["kind"] for p in progress] == ["reasoning", "sql"]
    assert progress[1]["sql"].startswith("SELECT region")


@pytest.mark.asyncio
async def test_returns_report_with_sources_and_posts_an_agent_mode_prompt():
    ws = _ws("c1")

    text = await genie_agent_tool("space-1")(question="revenue by region?", ws=ws)

    assert text == "West leads revenue.\n\nSources:\n- [sales](https://x/sales)"
    call = ws.api_client.do.call_args_list[0]
    assert call.args == ("POST", "/api/2.0/genie/agents/space-1/responses")
    assert call.kwargs["raw"] is True
    assert call.kwargs["headers"]["Accept"] == "text/event-stream"
    assert call.kwargs["body"] == {"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "revenue by region?"}]}]}
    assert ws.streams[0].closed


@pytest.mark.asyncio
async def test_structured_returns_citations_queries_and_tables():
    result = await genie_agent_tool("space-1", structured=True)(question="q", ws=_ws("c1"))

    assert result == {
        "question": "q",
        "report": "West leads revenue.",
        "citations": [{"type": "url_citation", "title": "sales", "url": "https://x/sales"}],
        "files": [],
        "queries": [{"call_id": "call1", "sql": "SELECT region, SUM(rev) FROM sales GROUP BY region",
                     "result": CALL_OUT["output"]}],
        "tables": [TABLE],
        "conversation_id": "c1",
        "response_id": "r-c1",
        "status": "completed",
    }


@pytest.mark.asyncio
async def test_without_a_progress_sink_the_final_report_still_returns():
    # No MLflow span is active, so emit_progress has nowhere to stream to.
    text = await genie_agent_tool("space-1")(question="q", ws=_ws("c1"))
    assert text.startswith("West leads revenue.")


@pytest.mark.asyncio
async def test_failed_response_is_a_tool_error():
    ws = MagicMock()
    failed = {"type": "response.failed", "sequence_number": 3, "response": _response(
        "c1", "failed", [], error={"code": "INTERNAL_ERROR", "message": "warehouse stopped"})}
    ws.api_client.do.return_value = {"contents": _Stream(_sse(_events(final=failed)))}

    with pytest.raises(ToolError, match="warehouse stopped"):
        await genie_agent_tool("space-1")(question="q", ws=ws)


@pytest.mark.asyncio
async def test_stream_ending_early_is_a_tool_error():
    ws = MagicMock()
    ws.api_client.do.return_value = {"contents": _Stream(_sse(_events()[:3]))}

    with pytest.raises(ToolError, match="status: in_progress"):
        await genie_agent_tool("space-1")(question="q", ws=ws)


@pytest.mark.asyncio
async def test_stale_obo_scope_becomes_the_reauthorize_hint():
    ws = MagicMock()
    ws.api_client.do.side_effect = PermissionDenied("Invalid scope, required scopes: genie")

    with pytest.raises(ToolError, match="Re-authorize the app"):
        await genie_agent_tool("space-1")(question="q", ws=ws)


@pytest.mark.asyncio
async def test_permission_denied_is_a_tool_error_not_a_500():
    ws = MagicMock()
    ws.api_client.do.side_effect = PermissionDenied("caller lacks CAN QUERY on the agent")

    with pytest.raises(ToolError, match="Genie Agent request failed: caller lacks CAN QUERY"):
        await genie_agent_tool("space-1")(question="q", ws=ws)


class _HangingStream(_Stream):
    """Sends ``response.created`` then blocks until closed, like a stalled response."""

    def __init__(self) -> None:
        super().__init__(_sse(_events()[:1]))
        self._closed = threading.Event()

    def read(self, n: int = -1) -> bytes:
        chunk = super().read(n)
        if chunk:
            return chunk
        self._closed.wait(5)
        raise OSError("stream closed")

    def close(self) -> None:
        super().close()
        self._closed.set()


@pytest.mark.asyncio
async def test_timeout_cancels_the_response_and_is_a_tool_error():
    ws = MagicMock()
    stream = _HangingStream()
    ws.api_client.do.side_effect = [{"contents": stream}, {}]

    with pytest.raises(ToolError, match="did not finish within 0s"):
        await genie_agent_tool("space-1", wait_s=0.2)(question="q", ws=ws)

    assert stream.closed
    cancel = ws.api_client.do.call_args_list[1]
    assert cancel.args == ("POST", "/api/2.0/genie/agents/space-1/conversations/c1/responses/r-c1/cancel")


def test_wait_s_and_space_id_are_validated():
    with pytest.raises(ValueError, match="positive wait_s"):
        genie_agent_tool("space-1", wait_s=0)
    with pytest.raises(ValueError, match="non-empty space_id"):
        genie_agent_tool("")


def test_declares_the_genie_space_and_scope():
    tool = genie_agent_tool("space-1")
    assert tool.__name__ == "ask_genie_agent"
    assert "genie" in tool._apx_user_api_scopes


# A report citing files in attached UC volumes (#1269). The Agent mode reference
# does not document the annotation schema, so the fixture covers both a typed
# file_citation and a citation identified only by its /Volumes/ path.
VOLUME_REPORT = {"type": "message", "role": "assistant", "id": "m2", "content": [
    {"type": "output_text", "text": "Q3 churn rose; the board deck explains why.",
     "annotations": [
         {"type": "file_citation", "path": "/Volumes/main/docs/board/q3.pdf", "page": 4,
          "snippet": "Churn rose 2 points", "title": "q3.pdf"},
         {"type": "citation", "url": "/Volumes/main/docs/board/notes.docx", "page_number": 2},
         {"type": "url_citation", "title": "churn", "url": "https://x/churn"},
     ]},
]}


def _ws_report(report: dict[str, Any]) -> MagicMock:
    ws = MagicMock()
    done = {"type": "response.completed", "sequence_number": 0,
            "response": _response("c1", "completed", [report])}
    ws.api_client.do.side_effect = [{"contents": _Stream(_sse([done]))}]
    return ws


@pytest.mark.asyncio
async def test_structured_maps_page_level_volume_file_citations():
    result = await genie_agent_tool("space-1", structured=True)(question="q", ws=_ws_report(VOLUME_REPORT))

    assert result["citations"] == VOLUME_REPORT["content"][0]["annotations"]
    assert result["files"] == [
        {"path": "/Volumes/main/docs/board/q3.pdf", "page": 4,
         "snippet": "Churn rose 2 points", "title": "q3.pdf"},
        {"path": "/Volumes/main/docs/board/notes.docx", "page": 2, "snippet": None, "title": None},
    ]


@pytest.mark.asyncio
async def test_text_sources_name_the_file_page_and_snippet():
    text = await genie_agent_tool("space-1")(question="q", ws=_ws_report(VOLUME_REPORT))

    assert text == (
        "Q3 churn rose; the board deck explains why.\n\nSources:\n"
        '- /Volumes/main/docs/board/q3.pdf, page 4: "Churn rose 2 points"\n'
        "- /Volumes/main/docs/board/notes.docx, page 2\n"
        "- [churn](https://x/churn)"
    )


FIXTURES = Path(__file__).parent / "fixtures"
HANDBOOK = "/Volumes/main/docs/handbooks/nimbus_handbook.pdf"


def _ws_fixture(name: str) -> MagicMock:
    """A client streaming the sanitized live Agent mode events in ``fixtures/<name>``."""
    events = json.loads((FIXTURES / name).read_text())["events"]
    ws = MagicMock()
    ws.api_client.do.side_effect = [{"contents": _Stream(_sse(events))}]
    return ws


@pytest.mark.asyncio
async def test_live_volume_answer_maps_explorer_link_and_context_chunk_to_files():
    """Live shape (#1281): no annotations, an explorer link in the report, a search chunk."""
    result = await genie_agent_tool("space-1", structured=True)(
        question="q", ws=_ws_fixture("genie_agent_volume_answer.json"))

    assert result["citations"] == []
    [file] = result["files"]
    assert file["path"] == HANDBOOK
    assert file["page"] == 1
    assert file["snippet"].startswith("This handbook is a synthetic test document")
    assert len(file["snippet"]) <= 300
    assert file["title"] is None


@pytest.mark.asyncio
async def test_live_volume_answer_text_has_a_sources_line():
    text = await genie_agent_tool("space-1")(question="q", ws=_ws_fixture("genie_agent_volume_answer.json"))

    report, sources = text.split("\n\nSources:\n")
    assert "ZIRCON-4419" in report
    assert sources.startswith(f'- {HANDBOOK}, page 1: "This handbook is a synthetic')


@pytest.mark.parametrize("chunk, page", [
    ('<chunk source="a.pdf (p.3)">page_number: Page 1 of 3\nbody</chunk>', 3),  # source wins
    ('<chunk source="a.pdf">page_number: Page 2 of 3\nbody</chunk>', 2),
    ('<chunk source="a.pdf">no page here</chunk>', None),
    ('<chunk source="a.pdf (p.x)">page_number: Page two</chunk>', None),
])
def test_chunk_page_is_best_effort(chunk, page):
    [file] = g._context_chunks(chunk)
    assert file["page"] == page
    assert file["path"].startswith("a.pdf")


def test_chunks_without_a_source_and_non_explorer_links_are_ignored():
    assert g._context_chunks('<chunk rank="1">x</chunk>') == []
    assert g._context_chunks(None) == []
    assert g._explorer_files("[a](https://h/explore/data/tables/c/s/t) [b](https://h/explore/data/volumes/c/s/v)") == []


def test_chunk_only_file_keeps_its_name_and_a_second_page_is_its_own_entry():
    files = g._volume_files(
        [], "[1](https://h/explore/data/volumes/c/s/v?filePreviewPath=dir%2Fa.pdf)",
        [*g._context_chunks('<chunk source="a.pdf (p.1)">x</chunk><chunk source="a.pdf (p.2)">y</chunk>'),
         *g._context_chunks('<chunk source="other.pdf">z</chunk>')],
    )
    assert [(f["path"], f["page"], f["snippet"]) for f in files] == [
        ("/Volumes/c/s/v/dir/a.pdf", 1, "x"),
        ("/Volumes/c/s/v/dir/a.pdf", 2, "y"),
        ("other.pdf", None, "z"),
    ]


@pytest.mark.asyncio
async def test_volume_index_not_ready_is_a_retryable_indexing_error():
    with pytest.raises(ToolError) as exc:
        await genie_agent_tool("space-1")(
            question="q", ws=_ws_fixture("genie_agent_volume_index_not_ready.json"))

    message = str(exc.value)
    assert "volume_index_not_ready" in message
    assert "still indexing" in message
    assert "several minutes" in message and "retryable" in message


def test_declared_volumes_are_uc_volume_resources_in_the_apps_manifest():
    from apx_agent._resources import ResourceSpec, get_resources, resources_to_databricks_yml

    tool = genie_agent_tool("space-1", volumes=["main.docs.board", "main.docs.contracts"])

    assert get_resources(tool) == [
        ResourceSpec("genie_space", "space-1"),
        ResourceSpec("uc_volume", "main.docs.board"),
        ResourceSpec("uc_volume", "main.docs.contracts"),
    ]
    assert tool._apx_user_api_scopes == ["genie"]
    assert "attached volumes" in tool.__doc__
    volume_entries = [e["uc_securable"] for e in resources_to_databricks_yml(get_resources(tool))
                      if "uc_securable" in e]
    assert volume_entries == [
        {"securable_full_name": "main.docs.board", "securable_type": "VOLUME", "permission": "READ_VOLUME"},
        {"securable_full_name": "main.docs.contracts", "securable_type": "VOLUME", "permission": "READ_VOLUME"},
    ]


@pytest.mark.parametrize("volumes, match", [
    ("main.docs.board", "must be a list"),
    (["main.docs"], "catalog.schema.volume"),
    ([f"main.docs.v{i}" for i in range(11)], "at most 10 volumes"),
])
def test_rejects_malformed_or_too_many_volumes(volumes, match):
    with pytest.raises(ValueError, match=match):
        genie_agent_tool("space-1", volumes=volumes)


@pytest.mark.asyncio
async def test_follow_ups_continue_the_sessions_agent_conversation():
    ws = _ws("c1", "c1")
    tool = genie_agent_tool("space-1")

    _ACTIVE_SESSION.set("s1")
    await tool(question="revenue?", ws=ws)
    await tool(question="and last year?", ws=ws)

    assert "conversation_id" not in _body(ws, 0)
    assert _body(ws, 1)["conversation_id"] == "c1"


@pytest.mark.asyncio
async def test_sessions_and_follow_ups_off_get_new_conversations():
    ws = _ws("c1", "c2", "c3")
    tool = genie_agent_tool("space-1")
    _ACTIVE_SESSION.set("s1")
    await tool(question="q", ws=ws)
    _ACTIVE_SESSION.set("s2")
    await tool(question="q", ws=ws)
    await genie_agent_tool("space-1", follow_ups=False)(question="q", ws=ws)

    assert all("conversation_id" not in _body(ws, i) for i in range(3))


@pytest.mark.asyncio
async def test_missing_agent_conversation_is_replaced(_fresh_registry):
    ws = _ws("c2")
    ws.api_client.do.side_effect = [NotFound("conversation gone"), *ws.api_client.do.side_effect]
    _fresh_registry.put("s1", "space-1", "c1", ws)
    _ACTIVE_SESSION.set("s1")

    text = await genie_agent_tool("space-1")(question="q", ws=ws)

    assert text.startswith("West leads revenue.")
    assert _body(ws, 0)["conversation_id"] == "c1"
    assert "conversation_id" not in _body(ws, 1)
    assert _fresh_registry.get("s1", "space-1") == "c2"


@pytest.mark.asyncio
async def test_ending_the_session_deletes_its_agent_conversation(monkeypatch):
    monkeypatch.setattr(g, "_CONVERSATIONS", g.GenieConversations())
    ws = _ws("c1")
    _ACTIVE_SESSION.set("s1")
    await genie_agent_tool("space-1")(question="q", ws=ws)

    assert end_genie_session("s1") == 1
    ws.genie.delete_conversation.assert_called_once_with(space_id="space-1", conversation_id="c1")
