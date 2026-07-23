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

    @pytest.mark.asyncio
    async def test_backslash_in_comment_is_escaped_no_breakout(self):
        """Trailing backslash in comment must be doubled — not left to escape the quote."""
        tool = _make_tool()
        ws = _fake_ws()
        captured = {}

        def _fake_run_sql(ws_arg, stmt, *, warehouse_id=None):
            captured["stmt"] = stmt
            return []

        with patch("apx_agent.uc_comment.run_sql", _fake_run_sql):
            result = await tool(table="orders", comment="x\\", ws=ws)

        assert result["status"] == "ok"
        # comment is one real backslash ("x\"); after escaping it becomes "x\\"
        # so the emitted SQL literal is: 'x\\'   (backslash doubled inside the quotes)
        assert captured["stmt"] == (
            "COMMENT ON TABLE `main`.`sales`.`orders` IS 'x\\\\'"
        )

    @pytest.mark.asyncio
    async def test_backslash_quote_breakout_neutralized(self):
        """Classic SQL-injection via backslash+quote must be neutralized."""
        tool = _make_tool()
        ws = _fake_ws()
        captured = {}

        def _fake_run_sql(ws_arg, stmt, *, warehouse_id=None):
            captured["stmt"] = stmt
            return []

        with patch("apx_agent.uc_comment.run_sql", _fake_run_sql):
            result = await tool(
                table="orders",
                comment="\\'; DROP TABLE x; --",
                ws=ws,
            )

        assert result["status"] == "ok"
        # comment is: \'; DROP TABLE x; --
        # after escaping: \\'' ; DROP TABLE x; --
        #   (backslash → \\, then single-quote → '')
        # so the full emitted statement ends with:  IS '\\'' ; DROP TABLE x; --'
        assert captured["stmt"] == (
            "COMMENT ON TABLE `main`.`sales`.`orders` IS "
            "'\\\\''; DROP TABLE x; --'"
        )
        # All single quotes in the emitted statement must be balanced (even count)
        assert captured["stmt"].count("'") % 2 == 0


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


# ---------------------------------------------------------------------------
# Integration: build via the declarative config path + opt-in assertion
# ---------------------------------------------------------------------------


class TestUcCommentWriterConfigIntegration:
    """Verify end-to-end: config-path build + real invocation + opt-in guarantee."""

    def test_build_via_load_config_tools(self):
        """load_config_tools dispatches type='uc_comment_writer' to uc_comment_tool."""
        from apx_agent._tool_config import load_config_tools

        tools = load_config_tools([
            {
                "type": "uc_comment_writer",
                "catalog": "main",
                "schema": "sales",
                "warehouse_id": "wh1",
            }
        ])
        assert len(tools) == 1
        assert callable(tools[0])
        assert tools[0].__name__ == "update_uc_comment"

    @pytest.mark.asyncio
    async def test_config_built_tool_writes_table_comment(self):
        """The config-built tool coroutine produces the correct SQL statement.

        This drives the exact same path as a real deployment: TOML entry →
        load_config_tools → uc_comment_tool → async invocation.
        """
        from apx_agent._tool_config import load_config_tools

        tools = load_config_tools([
            {
                "type": "uc_comment_writer",
                "catalog": "main",
                "schema": "sales",
                "warehouse_id": "wh1",
            }
        ])
        tool = tools[0]
        ws = MagicMock()
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
        assert captured["warehouse_id"] == "wh1"

    def test_opt_in_not_present_in_non_uc_comment_config(self):
        """uc_comment_writer is opt-in: declaring a different tool does not include it.

        Building from a non-empty [[tool.apx.tools]] config that omits
        uc_comment_writer must produce tools — proving the absence is meaningful
        (not a vacuous empty build) — without including update_uc_comment.
        """
        from apx_agent._tool_config import load_config_tools

        tools = load_config_tools([{"type": "sql", "warehouse_id": "wh1"}])
        names = [t.__name__ for t in tools]
        assert names, "sql config should produce at least one tool (absence is not vacuous)"
        assert "update_uc_comment" not in names, (
            "uc_comment_writer must not appear unless explicitly declared"
        )


class TestUcCommentToolBulk:
    """comments=[...] writes many statements in one governed call."""

    @pytest.mark.asyncio
    async def test_bulk_applies_all_rows(self):
        tool = _make_tool()
        ws = _fake_ws()
        stmts = []

        def _fake_run_sql(ws_arg, stmt, *, warehouse_id=None):
            stmts.append(stmt)
            return []

        with patch("apx_agent.uc_comment.run_sql", _fake_run_sql):
            result = await tool(
                ws=ws,
                comments=[
                    {"table": "orders", "comment": "One row per order."},
                    {"table": "orders", "column": "total_usd", "comment": "USD total."},
                ],
            )

        assert result["status"] == "ok"
        assert result["applied"] == 2
        assert result["failed"] == 0
        assert stmts == [
            "COMMENT ON TABLE `main`.`sales`.`orders` IS 'One row per order.'",
            "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `total_usd` COMMENT 'USD total.'",
        ]

    @pytest.mark.asyncio
    async def test_bulk_invalid_identifier_writes_nothing(self):
        tool = _make_tool()
        ws = _fake_ws()
        calls = []

        def _fake_run_sql(ws_arg, stmt, *, warehouse_id=None):
            calls.append(stmt)
            return []

        with patch("apx_agent.uc_comment.run_sql", _fake_run_sql):
            result = await tool(
                ws=ws,
                comments=[
                    {"table": "orders", "comment": "ok"},
                    {"table": "bad-name; DROP", "comment": "evil"},
                ],
            )

        assert result["status"] == "error"
        assert calls == [], "no statement may run when any identifier is invalid"

    @pytest.mark.asyncio
    async def test_bulk_partial_when_one_write_fails(self):
        tool = _make_tool()
        ws = _fake_ws()

        def _fake_run_sql(ws_arg, stmt, *, warehouse_id=None):
            if "returns" in stmt:
                raise RuntimeError("PERMISSION_DENIED: MODIFY")
            return []

        with patch("apx_agent.uc_comment.run_sql", _fake_run_sql):
            result = await tool(
                ws=ws,
                comments=[
                    {"table": "orders", "comment": "fine"},
                    {"table": "returns", "comment": "denied"},
                ],
            )

        assert result["status"] == "partial"
        assert result["applied"] == 1
        assert result["failed"] == 1
        assert [r["status"] for r in result["results"]] == ["ok", "error"]
