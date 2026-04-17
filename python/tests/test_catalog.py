"""Tests for catalog_tool(), lineage_tool(), and schema_tool() factories."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from apx_agent.catalog import catalog_tool, lineage_tool, schema_tool, uc_function_tool
from apx_agent._inspection import _inspect_tool_fn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_table(name: str, full_name: str, table_type: str = "MANAGED", comment: str = "") -> MagicMock:
    t = MagicMock()
    t.name = name
    t.full_name = full_name
    t.table_type = table_type
    t.comment = comment
    return t


def _make_column(name: str, type_name: str, type_text: str = "", comment: str = "", nullable: bool = True, position: int = 0) -> MagicMock:
    col = MagicMock()
    col.name = name
    col.type_name = type_name
    col.type_text = type_text
    col.comment = comment
    col.nullable = nullable
    col.position = position
    return col


# ---------------------------------------------------------------------------
# catalog_tool
# ---------------------------------------------------------------------------


class TestCatalogToolFactory:
    def test_default_name(self):
        tool = catalog_tool("main", "sales")
        assert tool.__name__ == "list_tables"

    def test_custom_name(self):
        tool = catalog_tool("main", "sales", name="list_sales_tables")
        assert tool.__name__ == "list_sales_tables"

    def test_description_contains_catalog_and_schema(self):
        tool = catalog_tool("main", "sales")
        assert "main" in (tool.__doc__ or "")
        assert "sales" in (tool.__doc__ or "")

    def test_custom_description(self):
        tool = catalog_tool("main", "sales", description="List my tables")
        assert tool.__doc__ == "List my tables"

    def test_is_coroutine(self):
        assert inspect.iscoroutinefunction(catalog_tool("main", "sales"))

    def test_no_plain_params(self):
        tool = catalog_tool("main", "sales")
        plain_params, dep_params = _inspect_tool_fn(tool)
        assert len(plain_params) == 0
        assert "ws" in dep_params

    @pytest.mark.asyncio
    async def test_returns_table_list(self):
        ws = MagicMock()
        ws.tables.list.return_value = [
            _make_table("orders", "main.sales.orders", "MANAGED", "Order records"),
            _make_table("customers", "main.sales.customers", "MANAGED"),
        ]
        tool = catalog_tool("main", "sales")
        result = await tool(ws=ws)
        assert len(result) == 2
        assert result[0]["full_name"] == "main.sales.orders"
        assert result[0]["comment"] == "Order records"

    @pytest.mark.asyncio
    async def test_calls_list_with_correct_catalog_schema(self):
        ws = MagicMock()
        ws.tables.list.return_value = []
        tool = catalog_tool("mycat", "myschema")
        await tool(ws=ws)
        ws.tables.list.assert_called_once_with(catalog_name="mycat", schema_name="myschema")

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        ws = MagicMock()
        ws.tables.list.side_effect = Exception("permission denied")
        tool = catalog_tool("main", "sales")
        result = await tool(ws=ws)
        assert result == []


# ---------------------------------------------------------------------------
# lineage_tool
# ---------------------------------------------------------------------------


class TestLineageToolFactory:
    def test_default_name(self):
        assert lineage_tool().__name__ == "get_table_lineage"

    def test_custom_name(self):
        assert lineage_tool(name="my_lineage").__name__ == "my_lineage"

    def test_is_coroutine(self):
        assert inspect.iscoroutinefunction(lineage_tool())

    def test_table_name_is_plain_param(self):
        tool = lineage_tool()
        plain_params, dep_params = _inspect_tool_fn(tool)
        assert "table_name" in plain_params
        assert plain_params["table_name"][0] is str
        assert "ws" in dep_params

    @pytest.mark.asyncio
    async def test_returns_upstreams_and_downstreams(self):
        ws = MagicMock()
        ws.api_client.do.return_value = {
            "upstreams": [{"tableInfo": {"name": "main.raw.events", "table_type": "MANAGED"}}],
            "downstreams": [{"tableInfo": {"name": "main.gold.summary", "table_type": "MANAGED"}}],
        }
        tool = lineage_tool()
        result = await tool(table_name="main.silver.cleaned", ws=ws)
        assert result["table"] == "main.silver.cleaned"
        assert result["upstreams"][0]["full_name"] == "main.raw.events"
        assert result["downstreams"][0]["full_name"] == "main.gold.summary"

    @pytest.mark.asyncio
    async def test_calls_lineage_api_with_table_name(self):
        ws = MagicMock()
        ws.api_client.do.return_value = {"upstreams": [], "downstreams": []}
        tool = lineage_tool()
        await tool(table_name="main.sales.orders", ws=ws)
        ws.api_client.do.assert_called_once_with(
            "GET",
            "/api/2.1/unity-catalog/lineage-tracking/table-lineage",
            query={"table_name": "main.sales.orders"},
        )

    @pytest.mark.asyncio
    async def test_handles_empty_lineage(self):
        ws = MagicMock()
        ws.api_client.do.return_value = {}
        tool = lineage_tool()
        result = await tool(table_name="main.sales.orders", ws=ws)
        assert result["upstreams"] == []
        assert result["downstreams"] == []

    @pytest.mark.asyncio
    async def test_skips_entries_without_table_info(self):
        ws = MagicMock()
        ws.api_client.do.return_value = {
            "upstreams": [{"notebookInfos": [{"notebook_id": 123}]}, {"tableInfo": {"name": "main.raw.x", "table_type": ""}}],
            "downstreams": [],
        }
        tool = lineage_tool()
        result = await tool(table_name="main.t", ws=ws)
        assert len(result["upstreams"]) == 1
        assert result["upstreams"][0]["full_name"] == "main.raw.x"


# ---------------------------------------------------------------------------
# schema_tool
# ---------------------------------------------------------------------------


class TestSchemaToolFactory:
    def test_default_name(self):
        assert schema_tool().__name__ == "describe_table"

    def test_custom_name(self):
        assert schema_tool(name="inspect_schema").__name__ == "inspect_schema"

    def test_is_coroutine(self):
        assert inspect.iscoroutinefunction(schema_tool())

    def test_table_name_is_plain_param(self):
        tool = schema_tool()
        plain_params, dep_params = _inspect_tool_fn(tool)
        assert "table_name" in plain_params
        assert plain_params["table_name"][0] is str
        assert "ws" in dep_params

    @pytest.mark.asyncio
    async def test_returns_column_list(self):
        ws = MagicMock()
        table = MagicMock()
        table.columns = [
            _make_column("id", "LONG", "BIGINT", "Primary key", False, 0),
            _make_column("name", "STRING", "STRING", "Customer name", True, 1),
        ]
        ws.tables.get.return_value = table
        tool = schema_tool()
        result = await tool(table_name="main.sales.customers", ws=ws)
        assert len(result) == 2
        assert result[0]["name"] == "id"
        assert result[0]["type"] == "LONG"
        assert result[0]["nullable"] is False
        assert result[1]["name"] == "name"
        assert result[1]["comment"] == "Customer name"

    @pytest.mark.asyncio
    async def test_calls_tables_get_with_full_name(self):
        ws = MagicMock()
        table = MagicMock()
        table.columns = []
        ws.tables.get.return_value = table
        tool = schema_tool()
        await tool(table_name="main.sales.orders", ws=ws)
        ws.tables.get.assert_called_once_with(full_name="main.sales.orders")

    @pytest.mark.asyncio
    async def test_handles_no_columns(self):
        ws = MagicMock()
        table = MagicMock()
        table.columns = None
        ws.tables.get.return_value = table
        tool = schema_tool()
        result = await tool(table_name="main.sales.empty", ws=ws)
        assert result == []


# ---------------------------------------------------------------------------
# uc_function_tool
# ---------------------------------------------------------------------------


def _make_func_info(params: list[tuple[str, str, int]], data_type: str = "STRING") -> MagicMock:
    """Build a mock function info object returned by ws.functions.get."""
    func = MagicMock()
    func.data_type = data_type
    param_mocks = []
    for name, type_name, position in params:
        p = MagicMock()
        p.name = name
        p.type_name = type_name
        p.position = position
        param_mocks.append(p)
    func.input_params.parameters = param_mocks
    return func


class TestUcFunctionToolFactory:
    def test_default_name_is_short_function_name(self):
        tool = uc_function_tool("main.tools.classify_intent")
        assert tool.__name__ == "classify_intent"

    def test_custom_name(self):
        tool = uc_function_tool("main.tools.classify_intent", name="classify")
        assert tool.__name__ == "classify"

    def test_function_name_in_default_description(self):
        tool = uc_function_tool("main.tools.classify_intent")
        assert "main.tools.classify_intent" in (tool.__doc__ or "")

    def test_custom_description(self):
        tool = uc_function_tool("main.tools.fn", description="My custom desc")
        assert tool.__doc__ == "My custom desc"

    def test_is_coroutine(self):
        assert inspect.iscoroutinefunction(uc_function_tool("main.tools.fn"))

    def test_params_is_plain_param(self):
        tool = uc_function_tool("main.tools.fn")
        plain_params, dep_params = _inspect_tool_fn(tool)
        assert "params" in plain_params
        assert "ws" in dep_params

    @pytest.mark.asyncio
    async def test_builds_correct_sql_for_string_params(self):
        ws = MagicMock()
        ws.functions.get.return_value = _make_func_info(
            [("text", "STRING", 0), ("lang", "STRING", 1)]
        )
        ws.warehouses.list.return_value = []
        # Patch run_sql to capture the SQL
        from apx_agent import catalog as cat_module
        calls = []
        original = cat_module.run_sql
        cat_module.run_sql = lambda ws, sql, **kw: calls.append(sql) or [{"classify_intent": "greeting"}]
        try:
            tool = uc_function_tool("main.tools.classify_intent")
            result = await tool(params={"text": "hello world", "lang": "en"}, ws=ws)
            assert calls[0] == "SELECT main.tools.classify_intent('hello world', 'en')"
            assert result == "greeting"
        finally:
            cat_module.run_sql = original

    @pytest.mark.asyncio
    async def test_quotes_strings_and_passes_numbers_raw(self):
        ws = MagicMock()
        ws.functions.get.return_value = _make_func_info(
            [("label", "STRING", 0), ("threshold", "DOUBLE", 1)]
        )
        from apx_agent import catalog as cat_module
        calls = []
        cat_module.run_sql = lambda ws, sql, **kw: calls.append(sql) or [{"r": "ok"}]
        try:
            tool = uc_function_tool("main.tools.fn")
            await tool(params={"label": "it's alive", "threshold": 0.9}, ws=ws)
            assert "'it''s alive'" in calls[0]
            assert "0.9" in calls[0]
        finally:
            cat_module.run_sql = lambda *a, **kw: []

    @pytest.mark.asyncio
    async def test_caches_function_definition(self):
        ws = MagicMock()
        ws.functions.get.return_value = _make_func_info([("x", "STRING", 0)])
        from apx_agent import catalog as cat_module
        cat_module.run_sql = lambda ws, sql, **kw: [{"fn": "a"}]
        tool = uc_function_tool("main.tools.fn")
        await tool(params={"x": "a"}, ws=ws)
        await tool(params={"x": "b"}, ws=ws)
        # functions.get should only be called once
        assert ws.functions.get.call_count == 1

    @pytest.mark.asyncio
    async def test_returns_table_result_for_multi_row(self):
        ws = MagicMock()
        ws.functions.get.return_value = _make_func_info([], data_type="TABLE_TYPE")
        from apx_agent import catalog as cat_module
        cat_module.run_sql = lambda ws, sql, **kw: [{"a": "1"}, {"a": "2"}]
        tool = uc_function_tool("main.tools.fn")
        result = await tool(params={}, ws=ws)
        assert result == [{"a": "1"}, {"a": "2"}]
