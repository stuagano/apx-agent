"""Root conftest.py — shared test fixtures and environment setup."""
import os

import pytest


@pytest.fixture(autouse=True)
def set_databricks_host(monkeypatch):
    """Ensure DATABRICKS_HOST is set for all tests that import app.py."""
    if not os.environ.get("DATABRICKS_HOST"):
        monkeypatch.setenv("DATABRICKS_HOST", "https://test.databricks.com")
