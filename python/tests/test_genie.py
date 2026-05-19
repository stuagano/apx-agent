"""Tests for ``genie_tool`` and ``genie_query_tool`` factories."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apx_agent import genie_query_tool, genie_tool
from apx_agent._inspection import _inspect_tool_fn


# ---------------------------------------------------------------------------
# Shared helpers for building fake GenieMessage / SDK responses
# ---------------------------------------------------------------------------

def _make_msg(
    *,
    status: str = "COMPLETED",
    text: str | None = None,
    sql: str | None = None,
    has_query_attachment: bool = False,
    error: str | None = None,
) -> MagicMock:
    """Build a fake ``GenieMessage`` matching the databricks-sdk shape."""
    from databricks.sdk.service.dashboards import MessageStatus

    msg = MagicMock()
    msg.status = getattr(MessageStatus, status)
    msg.error = error
    msg.conversation_id = "conv-1"
    msg.message_id = "msg-1"

    attachments = []
    if text is not None:
        text_att = MagicMock()
        text_att.text = MagicMock(content=text)
        text_att.query = None
        text_att.attachment_id = None
        attachments.append(text_att)
    if has_query_attachment:
        q_att = MagicMock()
        q_att.text = None
        q_att.query = MagicMock(query=sql or "SELECT 1")
        q_att.attachment_id = "att-1"
        attachments.append(q_att)
    msg.attachments = attachments
    return msg


def _make_ws(msg: MagicMock, *, rows: list[dict] | None = None) -> MagicMock:
    """Build a fake WorkspaceClient with genie + statement_response shape."""
    ws = MagicMock()
    ws.genie.start_conversation_and_wait.return_value = msg

    # Configure the query-result path
    qr = MagicMock()
    qr.statement_response = MagicMock()
    if rows:
        col_names = list(rows[0].keys()) if rows else []
        qr.statement_response.manifest.schema.columns = [
            MagicMock(name=n) for n in col_names
        ]
        # MagicMock name kwarg is special; fix it
        for col, n in zip(qr.statement_response.manifest.schema.columns, col_names):
            col.name = n
        qr.statement_response.result.data_array = [list(r.values()) for r in rows]
    else:
        qr.statement_response.manifest.schema.columns = []
        qr.statement_response.result.data_array = []
    ws.genie.get_message_query_result_by_attachment.return_value = qr
    return ws


# ===========================================================================
# genie_tool — narrative answer
# ===========================================================================

class TestGenieToolFactory:
    def test_returns_callable(self):
        assert callable(genie_tool("space-123"))

    def test_default_name(self):
        assert genie_tool("space-123").__name__ == "ask_genie"

    def test_custom_name(self):
        assert genie_tool("space-123", name="sales_data").__name__ == "sales_data"

    def test_default_description_mentions_space_id(self):
        assert "space-abc" in (genie_tool("space-abc").__doc__ or "")

    def test_inspection_exposes_question_only(self):
        plain, deps = _inspect_tool_fn(genie_tool("space-1"))
        assert list(plain.keys()) == ["question"]
        assert deps == ["ws"]


class TestGenieToolExecution:
    @pytest.mark.asyncio
    async def test_returns_text_on_completed(self):
        msg = _make_msg(status="COMPLETED", text="Revenue was $1M")
        ws = _make_ws(msg)
        tool = genie_tool("space-123")
        result = await tool(question="What was revenue?", ws=ws)
        assert result == "Revenue was $1M"

    @pytest.mark.asyncio
    async def test_calls_start_conversation_with_question_and_space(self):
        ws = _make_ws(_make_msg(status="COMPLETED", text="ok"))
        tool = genie_tool("space-123")
        await tool(question="Show me sales", ws=ws)

        ws.genie.start_conversation_and_wait.assert_called_once_with(
            space_id="space-123",
            content="Show me sales",
        )

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_no_text_attachment(self):
        msg = _make_msg(status="COMPLETED", text=None)
        ws = _make_ws(msg)
        tool = genie_tool("space-123")
        result = await tool(question="test", ws=ws)
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_failure_message_on_failed_status(self):
        msg = _make_msg(status="FAILED")
        ws = _make_ws(msg)
        tool = genie_tool("space-123")
        result = await tool(question="bad query", ws=ws)
        assert "did not complete" in result.lower()


# ===========================================================================
# genie_query_tool — structured results
# ===========================================================================

class TestGenieQueryToolFactory:
    def test_returns_callable(self):
        assert callable(genie_query_tool("space-123"))

    def test_default_name(self):
        assert genie_query_tool("space-123").__name__ == "query_genie"

    def test_custom_name(self):
        assert genie_query_tool("s", name="explore").__name__ == "explore"

    def test_default_description_mentions_space_id(self):
        assert "space-abc" in (genie_query_tool("space-abc").__doc__ or "")

    def test_inspection_exposes_question_only(self):
        plain, deps = _inspect_tool_fn(genie_query_tool("space-1"))
        assert list(plain.keys()) == ["question"]
        assert deps == ["ws"]


class TestGenieQueryToolExecution:
    @pytest.mark.asyncio
    async def test_returns_structured_dict_on_completed(self):
        msg = _make_msg(
            status="COMPLETED",
            text="Top products: A, B, C",
            sql="SELECT * FROM products ORDER BY rank LIMIT 3",
            has_query_attachment=True,
        )
        rows = [
            {"product_id": "A", "rank": 1},
            {"product_id": "B", "rank": 2},
            {"product_id": "C", "rank": 3},
        ]
        ws = _make_ws(msg, rows=rows)

        tool = genie_query_tool("space-123")
        result = await tool(question="Top products?", ws=ws)

        assert result["question"] == "Top products?"
        assert result["result_count"] == 3
        assert result["sql_results"] == rows
        assert result["generated_sql"].startswith("SELECT")
        assert "Top products" in result["genie_response"]

    @pytest.mark.asyncio
    async def test_returns_error_dict_on_failed_status(self):
        msg = _make_msg(status="FAILED", error="syntax error")
        ws = _make_ws(msg)
        tool = genie_query_tool("space-123")
        result = await tool(question="bad", ws=ws)
        assert "error" in result
        assert "FAILED" in result["error"]

    @pytest.mark.asyncio
    async def test_truncates_rows_to_max_rows(self):
        msg = _make_msg(status="COMPLETED", has_query_attachment=True, sql="SELECT 1")
        rows = [{"i": i} for i in range(50)]
        ws = _make_ws(msg, rows=rows)

        tool = genie_query_tool("space-123", max_rows=5)
        result = await tool(question="lots", ws=ws)
        assert len(result["sql_results"]) == 5
        assert result["result_count"] == 50  # the truncation doesn't lie about the total

    @pytest.mark.asyncio
    async def test_handles_genie_sdk_exception(self):
        ws = MagicMock()
        ws.genie.start_conversation_and_wait.side_effect = RuntimeError("network down")
        tool = genie_query_tool("space-123")
        result = await tool(question="anything", ws=ws)
        assert "error" in result
        assert "network down" in result["error"]
