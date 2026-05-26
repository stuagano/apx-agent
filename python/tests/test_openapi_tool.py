"""Tests for openapi_tool — fan out an OpenAPI spec into many http_tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from apx_agent import ResourceSpec, openapi_tool
from apx_agent._resources import get_resources

# A small OpenAPI 3.x spec with 3 operations across GET/POST.
_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Demo", "version": "1.0.0"},
    "paths": {
        "/users": {
            "get": {"operationId": "listUsers", "summary": "List users."},
            "post": {"operationId": "createUser", "summary": "Create a user."},
        },
        "/users/{id}": {
            "get": {"operationId": "getUser", "description": "Fetch one user."},
        },
    },
}


def _write_spec(tmp_path, spec=_SPEC, name="spec.json"):
    p = tmp_path / name
    p.write_text(json.dumps(spec), encoding="utf-8")
    return str(p)


class TestFanOut:
    def test_returns_one_tool_per_operation(self, tmp_path):
        tools = openapi_tool(_write_spec(tmp_path), connection="my_api")
        assert len(tools) == 3
        names = {t.__name__ for t in tools}
        assert names == {"listUsers", "createUser", "getUser"}

    def test_each_tool_declares_connection_resource(self, tmp_path):
        tools = openapi_tool(_write_spec(tmp_path), connection="my_api")
        for t in tools:
            assert ResourceSpec("uc_connection", "my_api") in get_resources(t)

    def test_warehouse_forwarded_to_each_tool(self, tmp_path):
        tools = openapi_tool(
            _write_spec(tmp_path), connection="my_api", warehouse_id="wh-1"
        )
        for t in tools:
            kinds = {(s.kind, s.identifier) for s in get_resources(t)}
            assert ("sql_warehouse", "wh-1") in kinds

    def test_empty_spec_returns_empty_list(self, tmp_path):
        path = _write_spec(tmp_path, {"openapi": "3.0.0", "paths": {}})
        assert openapi_tool(path, connection="my_api") == []


class TestIncludeFilter:
    def test_include_narrows_by_operation_id(self, tmp_path):
        tools = openapi_tool(
            _write_spec(tmp_path), connection="my_api", include=["createUser"]
        )
        assert {t.__name__ for t in tools} == {"createUser"}

    def test_include_narrows_by_path(self, tmp_path):
        tools = openapi_tool(
            _write_spec(tmp_path), connection="my_api", include=["{id}"]
        )
        assert {t.__name__ for t in tools} == {"getUser"}

    def test_include_none_keeps_all(self, tmp_path):
        tools = openapi_tool(_write_spec(tmp_path), connection="my_api", include=None)
        assert len(tools) == 3


class TestSlugFallback:
    def test_operation_id_missing_uses_slug(self, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "paths": {"/health/check": {"get": {"summary": "Health."}}},
        }
        tools = openapi_tool(_write_spec(tmp_path, spec), connection="my_api")
        assert len(tools) == 1
        # Slug of "get_/health/check" → "get_health_check"
        assert tools[0].__name__ == "get_health_check"


class TestUrlSpec:
    def test_url_spec_fetched_via_httpx(self):
        resp = MagicMock()
        resp.text = json.dumps(_SPEC)
        with patch("httpx.get", MagicMock(return_value=resp)) as m:
            tools = openapi_tool(
                "https://example.com/openapi.json", connection="my_api"
            )
        assert m.call_args.args[0] == "https://example.com/openapi.json"
        assert len(tools) == 3


class TestYamlSpec:
    def test_yaml_spec_parsed(self, tmp_path):
        # pyyaml is available in this environment — a YAML spec should parse.
        yaml = pytest.importorskip("yaml")
        path = tmp_path / "spec.yaml"
        path.write_text(yaml.safe_dump(_SPEC), encoding="utf-8")
        tools = openapi_tool(str(path), connection="my_api")
        assert {t.__name__ for t in tools} == {"listUsers", "createUser", "getUser"}
