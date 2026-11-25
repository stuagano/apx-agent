"""Tests for http_tool — external HTTP calls via the UC HTTP connection proxy."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from apx_agent import ResourceSpec, http_tool
from apx_agent._resources import (
    get_resources,
    get_user_api_scopes,
    resources_to_databricks_yml,
)
from apx_agent.http_tools import connection_proxy_url

_HOST = "https://ws.example.com"
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _ws(token: str = "dbx-user-token") -> MagicMock:
    ws = MagicMock()
    ws.config.host = _HOST + "/"
    ws.config.authenticate.return_value = {"Authorization": f"Bearer {token}"}
    return ws


def _mock_proxy(handler):
    """Route the tool's httpx client through ``handler`` (no network)."""

    def factory(**kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return patch("apx_agent.http_tools.httpx.AsyncClient", factory)


def _recording(status: int = 200, text: str = "ok"):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text=text)

    return seen, handler


class TestProxyUrl:
    def test_bare_connection(self):
        assert connection_proxy_url(_HOST, "my_api", "/users") == (
            f"{_HOST}/api/2.0/unity-catalog/connections/my_api/proxy/users"
        )

    def test_trailing_host_slash_and_missing_leading_path_slash(self):
        assert connection_proxy_url(_HOST + "/", "my_api", "users") == (
            f"{_HOST}/api/2.0/unity-catalog/connections/my_api/proxy/users"
        )

    def test_empty_path_targets_proxy_root(self):
        assert connection_proxy_url(_HOST, "my_api", "") == (
            f"{_HOST}/api/2.0/unity-catalog/connections/my_api/proxy/"
        )

    def test_three_part_name_kept_as_one_segment(self):
        assert connection_proxy_url(_HOST, "main.conns.slack", "/chat.postMessage") == (
            f"{_HOST}/api/2.0/unity-catalog/connections/main.conns.slack/proxy/chat.postMessage"
        )

    def test_name_cannot_escape_its_path_segment(self):
        url = connection_proxy_url(_HOST, "../../clusters/x?y", "/z")
        assert url == (
            f"{_HOST}/api/2.0/unity-catalog/connections/..%2F..%2Fclusters%2Fx%3Fy/proxy/z"
        )


class TestFactoryShape:
    def test_name_and_description(self):
        t = http_tool("my_api", method="get", path="/users", name="list_users")
        assert t.__name__ == "list_users"
        assert "my_api" in t.__doc__ and "/users" in t.__doc__

    def test_explicit_description_wins(self):
        t = http_tool("my_api", description="Custom.", name="x")
        assert t.__doc__ == "Custom."

    def test_declares_only_connection_resource(self):
        t = http_tool("my_api", name="x")
        assert get_resources(t) == [ResourceSpec("uc_connection", "my_api")]

    def test_declares_catalog_connections_scope(self):
        t = http_tool("my_api", name="x")
        assert get_user_api_scopes(t) == ["catalog.connections"]

    def test_connection_compiles_to_use_connection_app_resource(self):
        t = http_tool("main.conns.slack", name="x")
        (entry,) = resources_to_databricks_yml(get_resources(t))
        assert entry["uc_securable"] == {
            "securable_full_name": "main.conns.slack",
            "securable_type": "CONNECTION",
            "permission": "USE_CONNECTION",
        }

    def test_warehouse_id_deprecated_and_not_declared(self):
        with pytest.warns(DeprecationWarning, match="connection proxy"):
            t = http_tool("my_api", warehouse_id="wh-1", name="x")
        assert "sql_warehouse" not in {s.kind for s in get_resources(t)}

    def test_invalid_method_rejected(self):
        with pytest.raises(ValueError, match="method must be one of"):
            http_tool("my_api", method="TRACE", name="x")


class TestCallMechanics:
    @pytest.mark.asyncio
    async def test_get_goes_to_proxy_with_databricks_token(self):
        t = http_tool("my_api", method="GET", path="/users", name="x")
        seen, handler = _recording(200, "ok")
        with _mock_proxy(handler):
            out = await t(ws=_ws("dbx-user-token"))
        assert out == {"status_code": 200, "text": "ok"}
        (req,) = seen
        assert req.method == "GET"
        assert str(req.url) == f"{_HOST}/api/2.0/unity-catalog/connections/my_api/proxy/users"
        assert req.headers["Authorization"] == "Bearer dbx-user-token"
        assert req.content == b""

    @pytest.mark.asyncio
    async def test_query_encoded_as_url_params(self):
        t = http_tool("my_api", method="GET", path="/search", name="x")
        seen, handler = _recording(200, "[]")
        with _mock_proxy(handler):
            await t(query={"q": "a&b=c", "limit": "10"}, ws=_ws())
        params = dict(seen[0].url.params)
        assert params == {"q": "a&b=c", "limit": "10"}
        assert seen[0].url.path == "/api/2.0/unity-catalog/connections/my_api/proxy/search"

    @pytest.mark.asyncio
    async def test_body_sent_as_json(self):
        t = http_tool("my_api", method="POST", path="/items", name="x")
        seen, handler = _recording(201, "{}")
        with _mock_proxy(handler):
            out = await t(body={"name": "widget", "qty": 3}, ws=_ws())
        assert out["status_code"] == 201
        assert seen[0].method == "POST"
        assert seen[0].headers["Content-Type"] == "application/json"
        assert json.loads(seen[0].content) == {"name": "widget", "qty": 3}

    @pytest.mark.asyncio
    async def test_non_2xx_passed_through(self):
        t = http_tool("my_api", path="/x", name="x")
        _, handler = _recording(403, "USE CONNECTION required")
        with _mock_proxy(handler):
            out = await t(ws=_ws())
        assert out == {"status_code": 403, "text": "USE CONNECTION required"}

    @pytest.mark.asyncio
    async def test_truncates_long_text(self):
        t = http_tool("my_api", path="/big", name="x", max_chars=10)
        _, handler = _recording(200, "a" * 100)
        with _mock_proxy(handler):
            out = await t(ws=_ws())
        assert out["text"].startswith("a" * 10)
        assert "truncated" in out["text"]

    @pytest.mark.asyncio
    async def test_transport_error_returned_not_raised(self):
        t = http_tool("my_api", path="/x", name="x")

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        with _mock_proxy(handler):
            out = await t(ws=_ws())
        assert out == {"error": "boom"}

    @pytest.mark.asyncio
    async def test_no_sql_warehouse_used(self):
        t = http_tool("my_api", path="/x", name="x")
        _, handler = _recording()
        with _mock_proxy(handler), patch("apx_agent._sql.run_sql") as run_sql:
            await t(ws=_ws())
        run_sql.assert_not_called()


class TestOpenapiSpecGuard:
    def test_non_spec_response_raises_valueerror_not_attributeerror(self):
        """A non-JSON/text response (e.g. an error page) yaml-parses to a str.

        Regression: `(doc or {}).get("paths")` then raised AttributeError
        ('str' object has no attribute 'get'). It must be a clean ValueError.
        """
        from apx_agent.http_tools import openapi_tool

        resp = MagicMock()
        resp.text = "Service Unavailable"  # yaml-parses to the bare string
        resp.raise_for_status = MagicMock()  # simulate a 200 with a bad body
        with patch("httpx.get", return_value=resp):
            with pytest.raises(ValueError):
                openapi_tool("https://example.com/not-a-spec", "conn")
