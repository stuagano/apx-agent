"""Tests for new Dependencies members — Request and Sql."""

from __future__ import annotations

from typing import Annotated, get_args, get_origin

from fastapi import params

from apx_agent import Dependencies
from apx_agent._defaults import RequestDependency, SqlDependency


class TestDependenciesRequest:
    def test_is_annotated_depends(self):
        """Dependencies.Request should be an Annotated[Request, Depends(...)]."""
        assert get_origin(RequestDependency) is Annotated
        args = get_args(RequestDependency)
        assert any(isinstance(a, params.Depends) for a in args)

    def test_excluded_from_tool_schema(self):
        """A tool param typed as Dependencies.Request should be treated as a dep."""
        from apx_agent._inspection import _is_fastapi_dependency

        assert _is_fastapi_dependency(Dependencies.Request)


class TestDependenciesSql:
    def test_is_annotated_depends(self):
        """Dependencies.Sql should be an Annotated[SqlRunnerFn, Depends(...)]."""
        assert get_origin(SqlDependency) is Annotated
        args = get_args(SqlDependency)
        assert any(isinstance(a, params.Depends) for a in args)

    def test_excluded_from_tool_schema(self):
        """A tool param typed as Dependencies.Sql should be treated as a dep."""
        from apx_agent._inspection import _is_fastapi_dependency

        assert _is_fastapi_dependency(Dependencies.Sql)


class TestToolSchemaExclusion:
    """Verify that Request/Sql params don't leak into the tool's input schema."""

    def test_request_param_excluded(self):
        from apx_agent._inspection import _inspect_tool_fn

        def my_tool(query: str, request: Dependencies.Request) -> str:  # type: ignore[valid-type]
            """A tool."""
            return query

        plain, deps = _inspect_tool_fn(my_tool)
        assert "query" in plain
        assert "request" not in plain
        assert "request" in deps

    def test_sql_param_excluded(self):
        from apx_agent._inspection import _inspect_tool_fn

        def my_tool(table: str, sql: Dependencies.Sql) -> list:  # type: ignore[valid-type]
            """A tool."""
            return []

        plain, deps = _inspect_tool_fn(my_tool)
        assert "table" in plain
        assert "sql" not in plain
        assert "sql" in deps
