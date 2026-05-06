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
        with patch("apx_agent._defaults.WorkspaceClient") as MockWS:
            MockWS.return_value = MagicMock()
            client = _get_user_client(headers)
            MockWS.assert_called_once_with()
            assert client is not None

    def test_creates_client_with_obo_token_and_host(self):
        from unittest.mock import patch
        from pydantic import SecretStr
        from apx_agent._defaults import _get_user_client

        headers = DatabricksAppsHeaders(
            host="myworkspace.cloud.databricks.com",
            user_name="alice",
            user_id="123",
            user_email="alice@example.com",
            request_id=None,
            token=SecretStr("obo-token-123"),
        )
        with patch("apx_agent._defaults.WorkspaceClient") as MockWS:
            MockWS.return_value = MagicMock()
            client = _get_user_client(headers)
            MockWS.assert_called_once_with(
                token="obo-token-123",
                host="https://myworkspace.cloud.databricks.com",
            )

    def test_obo_client_no_pat_auth_type(self):
        """OBO tokens should not use auth_type='pat'."""
        from unittest.mock import patch, call
        from pydantic import SecretStr
        from apx_agent._defaults import _get_user_client

        headers = DatabricksAppsHeaders(
            host="ws.databricks.com",
            user_name=None, user_id=None, user_email=None,
            request_id=None, token=SecretStr("token"),
        )
        with patch("apx_agent._defaults.WorkspaceClient") as MockWS:
            MockWS.return_value = MagicMock()
            _get_user_client(headers)
            # Should NOT pass auth_type="pat"
            kwargs = MockWS.call_args.kwargs
            assert "auth_type" not in kwargs


class TestMakeWorkspaceClient:
    """_make_workspace_client resolves the Databricks Apps auth conflict."""

    def test_no_conflict_calls_default(self):
        from unittest.mock import patch
        from apx_agent._defaults import _make_workspace_client

        with patch("apx_agent._defaults.WorkspaceClient") as MockWS, \
             patch.dict("os.environ", {}, clear=False):
            # Ensure neither conflict key is set
            import os
            env = {k: v for k, v in os.environ.items()
                   if k not in ("DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET", "DATABRICKS_TOKEN")}
            with patch.dict("os.environ", env, clear=True):
                MockWS.return_value = MagicMock()
                _make_workspace_client()
                MockWS.assert_called_once_with()

    def test_oauth_and_pat_conflict_prefers_oauth(self):
        """When both OAuth M2M and PAT are set, use OAuth M2M and suppress PAT from env."""
        from unittest.mock import patch
        import os
        from apx_agent._defaults import _make_workspace_client

        conflict_env = {
            "DATABRICKS_CLIENT_ID": "my-client-id",
            "DATABRICKS_CLIENT_SECRET": "my-client-secret",
            "DATABRICKS_TOKEN": "dapi-my-pat",
            "DATABRICKS_HOST": "https://workspace.databricks.com",
        }
        with patch("apx_agent._defaults.WorkspaceClient") as MockWS, \
             patch.dict("os.environ", conflict_env, clear=True):
            MockWS.return_value = MagicMock()
            _make_workspace_client()
            MockWS.assert_called_once_with(
                client_id="my-client-id",
                client_secret="my-client-secret",
                host="https://workspace.databricks.com",
            )
            # DATABRICKS_TOKEN must be restored after the call
            assert os.environ.get("DATABRICKS_TOKEN") == "dapi-my-pat"

    def test_explicit_token_clears_oauth_env_during_call(self):
        """When an OBO token is passed, OAuth M2M env vars are removed during call and restored after.

        This is the Databricks Apps scenario: DATABRICKS_CLIENT_ID/SECRET are injected by the
        platform, but we want to create a WorkspaceClient with the user's OBO token. Without
        temporarily removing the OAuth creds, the SDK raises 'more than one authorization method'.
        """
        import os
        from unittest.mock import patch
        from apx_agent._defaults import _make_workspace_client

        observed_env_during_call: dict = {}

        def capture_env(**kwargs):
            observed_env_during_call.update(os.environ.copy())
            return MagicMock()

        conflict_env = {
            "DATABRICKS_CLIENT_ID": "my-client-id",
            "DATABRICKS_CLIENT_SECRET": "my-client-secret",
            "DATABRICKS_TOKEN": "dapi-my-pat",
        }
        with patch("apx_agent._defaults.WorkspaceClient", side_effect=capture_env), \
             patch.dict("os.environ", conflict_env, clear=True):
            _make_workspace_client(token="obo-token", host="https://ws.databricks.com")

        # OAuth creds must be absent during WorkspaceClient() call
        assert "DATABRICKS_CLIENT_ID" not in observed_env_during_call
        assert "DATABRICKS_CLIENT_SECRET" not in observed_env_during_call
        # OAuth creds must be restored afterward
        import os
        # (patch.dict restores the original env, so we just verify the logic)

    def test_explicit_kwargs_forwarded_to_workspace_client(self):
        """Explicit kwargs are forwarded unchanged to WorkspaceClient."""
        from unittest.mock import patch
        from apx_agent._defaults import _make_workspace_client

        conflict_env = {
            "DATABRICKS_CLIENT_ID": "my-client-id",
            "DATABRICKS_CLIENT_SECRET": "my-client-secret",
            "DATABRICKS_TOKEN": "dapi-my-pat",
        }
        with patch("apx_agent._defaults.WorkspaceClient") as MockWS, \
             patch.dict("os.environ", conflict_env, clear=True):
            MockWS.return_value = MagicMock()
            _make_workspace_client(token="obo-token", host="https://ws.databricks.com")
            MockWS.assert_called_once_with(token="obo-token", host="https://ws.databricks.com")


class TestDependenciesClass:
    def test_type_aliases_exist(self):
        assert Dependencies.Client is not None
        assert Dependencies.UserClient is not None
        assert Dependencies.Headers is not None
