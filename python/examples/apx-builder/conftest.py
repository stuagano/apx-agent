"""Root conftest.py — shared test fixtures and environment setup."""
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def set_databricks_host(monkeypatch):
    """Ensure DATABRICKS_HOST is set for all tests that import app.py."""
    if not os.environ.get("DATABRICKS_HOST"):
        monkeypatch.setenv("DATABRICKS_HOST", "https://test.databricks.com")


@pytest.fixture(autouse=True)
def mock_workspace_client():
    """Patch WorkspaceClient so tests don't make real network calls on init."""
    mock_ws = MagicMock()
    mock_ws.current_user.me.return_value.user_name = "test@example.com"
    with patch("app.WorkspaceClient", return_value=mock_ws):
        yield mock_ws
