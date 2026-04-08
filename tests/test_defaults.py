"""Tests for _defaults.py — dependency injection and header extraction."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import UUID

import pytest

from apx_agent._defaults import (
    Dependencies,
    DatabricksAppsHeaders,
    get_databricks_headers,
)


class TestGetDatabricksHeaders:
    def test_all_headers_present(self):
        headers = get_databricks_headers(
            host="workspace.cloud.databricks.com",
            user_name="alice",
            user_id="12345",
            user_email="alice@example.com",
            request_id="550e8400-e29b-41d4-a716-446655440000",
            token="dapi-fake-token",
        )
        assert headers.host == "workspace.cloud.databricks.com"
        assert headers.user_name == "alice"
        assert headers.user_id == "12345"
        assert headers.user_email == "alice@example.com"
        assert headers.request_id == UUID("550e8400-e29b-41d4-a716-446655440000")
        assert headers.token.get_secret_value() == "dapi-fake-token"

    def test_all_headers_missing(self):
        headers = get_databricks_headers()
        assert headers.host is None
        assert headers.user_name is None
        assert headers.user_id is None
        assert headers.user_email is None
        assert headers.request_id is None
        assert headers.token is None

    def test_partial_headers(self):
        headers = get_databricks_headers(host="example.com", user_email="test@test.com")
        assert headers.host == "example.com"
        assert headers.user_email == "test@test.com"
        assert headers.user_name is None

    def test_token_is_secret(self):
        headers = get_databricks_headers(token="secret-token")
        # SecretStr should not reveal the value in repr
        assert "secret-token" not in repr(headers.token)
        assert headers.token.get_secret_value() == "secret-token"


class TestGetWorkspaceClient:
    def test_returns_from_app_state(self):
        from apx_agent._defaults import _get_workspace_client

        mock_ws = MagicMock()
        request = MagicMock()
        request.app.state.workspace_client = mock_ws
        assert _get_workspace_client(request) is mock_ws


class TestGetUserClient:
    def test_falls_back_to_cli_without_token(self):
        from unittest.mock import patch
        from apx_agent._defaults import _get_user_client

        headers = DatabricksAppsHeaders(
            host=None, user_name=None, user_id=None,
            user_email=None, request_id=None, token=None,
        )
        # Without OBO token, falls back to CLI credentials (WorkspaceClient())
        with patch("apx_agent._defaults.WorkspaceClient") as MockWS:
            MockWS.return_value = MagicMock()
            client = _get_user_client(headers)
            MockWS.assert_called_once_with()
            assert client is not None


class TestDependenciesClass:
    def test_type_aliases_exist(self):
        # Verify the type aliases are accessible
        assert Dependencies.Client is not None
        assert Dependencies.UserClient is not None
        assert Dependencies.Headers is not None
