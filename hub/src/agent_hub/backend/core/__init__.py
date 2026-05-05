"""Thin compatibility shim — re-exports from the apx-agent package.

The agent hub is NOT an agent itself — it's the registry. We only
use apx_agent for the Dependencies injection layer.

Update: uv lock --upgrade-package apx-agent && uv sync
"""

from apx_agent import Dependencies as Dependencies  # noqa: F401
