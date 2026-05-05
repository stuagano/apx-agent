"""Thin compatibility shim — re-exports from the apx-agent package.

Update: uv lock --upgrade-package apx-agent && uv sync
"""

from apx_agent import Dependencies as Dependencies  # noqa: F401
from apx_agent import create_app as create_app  # noqa: F401
