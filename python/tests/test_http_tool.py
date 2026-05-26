"""Tests for http_tool — external HTTP calls via governed UC connections."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apx_agent import ResourceSpec, http_tool
from apx_agent._resources import get_resources


def _patch_run_sql(rows):
    """Patch run_sql (imported inside the factory from ._sql) to return rows.

    Returns the mock so tests can assert on the SQL + bind parameters passed.
    """
    return patch("apx_agent._sql.run_sql", MagicMock(return_value=rows))


class TestFactoryShape:
    def test_name_and_description(self):
        t = http_tool("my_api", method="get", path="/users", name="list_users")
        assert t.__name__ == "list_users"
        assert "my_api" in t.__doc__ and "/users" in t.__doc__

    def test_explicit_description_wins(self):
        t = http_tool("my_api", description="Custom.", name="x")
        assert t.__doc__ == "Custom."

    def test_declares_connection_resource(self):
        t = http_tool("my_api", name="x")
        specs = get_resources(t)
        assert ResourceSpec("uc_connection", "my_api") in specs

    def test_declares_warehouse_when_given(self):
        t = http_tool("my_api", warehouse_id="wh-1", name="x")
        kinds = {(s.kind, s.identifier) for s in get_resources(t)}
        assert ("uc_connection", "my_api") in kinds
        assert ("sql_warehouse", "wh-1") in kinds

    def test_no_warehouse_resource_when_omitted(self):
        t = http_tool("my_api", name="x")
        kinds = {s.kind for s in get_resources(t)}
        assert "sql_warehouse" not in kinds

    def test_invalid_method_rejected(self):
        with pytest.raises(ValueError, match="method must be one of"):
            http_tool("my_api", method="TRACE", name="x")


class TestCallMechanics:
    @pytest.mark.asyncio
    async def test_builds_http_request_sql(self):
        t = http_tool("my_api", method="GET", path="/users", name="x")
        with _patch_run_sql([{"status_code": 200, "text": "ok"}]) as m:
            out = await t(ws=MagicMock())
        assert out == {"status_code": 200, "text": "ok"}
        sql = m.call_args.args[1]
        assert "http_request(" in sql
        assert "CONN => 'my_api'" in sql
        assert "METHOD => 'GET'" in sql
        assert "PATH => '/users'" in sql

    @pytest.mark.asyncio
    async def test_query_passed_as_bind_params_not_interpolated(self):
        t = http_tool("my_api", method="GET", path="/search", name="x")
        with _patch_run_sql([{"status_code": 200, "text": "[]"}]) as m:
            await t(query={"q": "hello", "limit": "10"}, ws=MagicMock())
        sql = m.call_args.args[1]
        params = m.call_args.kwargs["parameters"]
        # Keys AND values are bound, never interpolated into the SQL text.
        assert "PARAMS => map(:k0, :v0, :k1, :v1)" in sql
        bound = {p["name"]: p["value"] for p in params}
        assert bound == {"k0": "q", "v0": "hello", "k1": "limit", "v1": "10"}

    @pytest.mark.asyncio
    async def test_body_passed_as_json_bind(self):
        t = http_tool("my_api", method="POST", path="/items", name="x")
        with _patch_run_sql([{"status_code": 201, "text": "{}"}]) as m:
            await t(body={"name": "widget", "qty": 3}, ws=MagicMock())
        sql = m.call_args.args[1]
        params = m.call_args.kwargs["parameters"]
        assert "JSON => :body" in sql
        body_param = next(p for p in params if p["name"] == "body")
        assert body_param["value"] == '{"name": "widget", "qty": 3}'

    @pytest.mark.asyncio
    async def test_no_params_when_no_query_or_body(self):
        t = http_tool("my_api", method="GET", path="/ping", name="x")
        with _patch_run_sql([{"status_code": 200, "text": "pong"}]) as m:
            await t(ws=MagicMock())
        assert m.call_args.kwargs["parameters"] is None
        assert "PARAMS =>" not in m.call_args.args[1]
        assert "JSON =>" not in m.call_args.args[1]

    @pytest.mark.asyncio
    async def test_connection_name_is_escaped(self):
        # A quote in the (developer-supplied) connection name must not break out.
        t = http_tool("ev'il", method="GET", path="/x", name="x")
        with _patch_run_sql([{"status_code": 200, "text": ""}]) as m:
            await t(ws=MagicMock())
        assert "CONN => 'ev''il'" in m.call_args.args[1]

    @pytest.mark.asyncio
    async def test_truncates_long_text(self):
        long = "a" * 100
        t = http_tool("my_api", path="/big", name="x", max_chars=10)
        with _patch_run_sql([{"status_code": 200, "text": long}]):
            out = await t(ws=MagicMock())
        assert out["text"].startswith("a" * 10)
        assert "truncated" in out["text"]

    @pytest.mark.asyncio
    async def test_error_returned_not_raised(self):
        t = http_tool("my_api", path="/x", name="x")
        with patch("apx_agent._sql.run_sql", MagicMock(side_effect=RuntimeError("boom"))):
            out = await t(ws=MagicMock())
        assert out["error"] == "boom"

    @pytest.mark.asyncio
    async def test_empty_result_set(self):
        t = http_tool("my_api", path="/x", name="x")
        with _patch_run_sql([]):
            out = await t(ws=MagicMock())
        assert out == {"status_code": None, "text": ""}

    @pytest.mark.asyncio
    async def test_warehouse_id_forwarded_to_run_sql(self):
        t = http_tool("my_api", path="/x", name="x", warehouse_id="wh-9")
        with _patch_run_sql([{"status_code": 200, "text": ""}]) as m:
            await t(ws=MagicMock())
        assert m.call_args.kwargs["warehouse_id"] == "wh-9"
