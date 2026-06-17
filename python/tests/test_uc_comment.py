"""Tests for uc_comment_tool — governed, opt-in UC COMMENT writer.

The inner coroutine produced by uc_comment_tool() is directly callable
(build_tool returns the same callable it receives, just name/doc-stamped).
We pass a fake ws and monkeypatch run_sql to capture the emitted statement.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(catalog="main", schema="sales", warehouse_id="wh-abc"):
    """Return the inner coroutine for the given catalog/schema."""
    from apx_agent.uc_comment import uc_comment_tool
    return uc_comment_tool(catalog=catalog, schema=schema, warehouse_id=warehouse_id)


def _fake_ws():
    return MagicMock()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestUcCommentToolTableComment:
    """Table-level COMMENT statement is generated correctly."""

    @pytest.mark.asyncio
    async def test_table_comment_statement_exact(self):
        tool = _make_tool()
        ws = _fake_ws()
        captured = {}

        def _fake_run_sql(ws_arg, stmt, *, warehouse_id=None):
            captured["stmt"] = stmt
            captured["warehouse_id"] = warehouse_id
            return []

        with patch("apx_agent.uc_comment.run_sql", _fake_run_sql):
            result = await tool(table="orders", comment="One row per order.", ws=ws)

        assert result["status"] == "ok"
        assert captured["stmt"] == (
            "COMMENT ON TABLE `main`.`sales`.`orders` IS 'One row per order.'"
        )
        assert captured["warehouse_id"] == "wh-abc"


class TestUcCommentToolColumnComment:
    """Column-level ALTER TABLE … ALTER COLUMN … COMMENT statement."""

    @pytest.mark.asyncio
    async def test_column_comment_statement_exact(self):
        tool = _make_tool()
        ws = _fake_ws()
        captured = {}

        def _fake_run_sql(ws_arg, stmt, *, warehouse_id=None):
            captured["stmt"] = stmt
            return []

        with patch("apx_agent.uc_comment.run_sql", _fake_run_sql):
            result = await tool(
                table="orders",
                comment="Order total in USD.",
                ws=ws,
                column="total_usd",
            )

        assert result["status"] == "ok"
        assert captured["stmt"] == (
            "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `total_usd` COMMENT 'Order total in USD.'"
        )


class TestUcCommentToolEscaping:
    """Single quotes in the comment text are doubled (SQL literal escaping)."""

    @pytest.mark.asyncio
    async def test_single_quote_is_doubled(self):
        tool = _make_tool()
        ws = _fake_ws()
        captured = {}

        def _fake_run_sql(ws_arg, stmt, *, warehouse_id=None):
            captured["stmt"] = stmt
            return []

        with patch("apx_agent.uc_comment.run_sql", _fake_run_sql):
            result = await tool(table="orders", comment="don't", ws=ws)

        assert result["status"] == "ok"
        assert "don''t" in captured["stmt"]


class TestUcCommentToolIdentifierValidation:
    """Invalid table identifiers return an error without calling run_sql."""

    @pytest.mark.asyncio
    async def test_invalid_table_identifier_returns_error(self):
        tool = _make_tool()
        ws = _fake_ws()
        called = []

        def _fake_run_sql(ws_arg, stmt, *, warehouse_id=None):
            called.append(stmt)
            return []

        with patch("apx_agent.uc_comment.run_sql", _fake_run_sql):
            result = await tool(
                table="orders; DROP TABLE x",
                comment="malicious",
                ws=ws,
            )

        assert result["status"] == "error"
        assert "orders; DROP TABLE x" in result["message"]
        assert called == [], "run_sql must NOT be called for invalid identifiers"

    @pytest.mark.asyncio
    async def test_invalid_column_identifier_returns_error(self):
        tool = _make_tool()
        ws = _fake_ws()
        called = []

        def _fake_run_sql(ws_arg, stmt, *, warehouse_id=None):
            called.append(stmt)
            return []

        with patch("apx_agent.uc_comment.run_sql", _fake_run_sql):
            result = await tool(
                table="orders",
                comment="ok",
                ws=ws,
                column="bad col!",
            )

        assert result["status"] == "error"
        assert called == [], "run_sql must NOT be called for invalid column identifiers"


class TestUcCommentToolErrorSurfacing:
    """run_sql raising an exception surfaces as status='error', not a raise."""

    @pytest.mark.asyncio
    async def test_permission_error_surfaced_not_raised(self):
        tool = _make_tool()
        ws = _fake_ws()

        def _raise_permission(ws_arg, stmt, *, warehouse_id=None):
            raise PermissionError("User lacks MODIFY grant on table")

        with patch("apx_agent.uc_comment.run_sql", _raise_permission):
            result = await tool(table="orders", comment="test", ws=ws)

        assert result["status"] == "error"
        assert "MODIFY" in result["message"]
        # The statement that was attempted should also be surfaced
        assert "statement" in result
