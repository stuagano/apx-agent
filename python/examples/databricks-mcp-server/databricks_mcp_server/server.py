"""
Databricks MCP Server

A FastMCP server that exposes Databricks operations as MCP tools.
Simply wraps functions from databricks-tools-core.
"""

import functools
import subprocess
import sys
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from .middleware import TimeoutHandlingMiddleware


# ---------------------------------------------------------------------------
# Windows fixes — must run BEFORE FastMCP init and tool registration
# ---------------------------------------------------------------------------


def _patch_subprocess_stdin():
    """Monkey-patch subprocess so stdin defaults to DEVNULL on Windows.

    When the MCP server runs in stdio mode, stdin IS the JSON-RPC pipe.
    Any subprocess call without explicit stdin lets child processes inherit
    this pipe handle. On Windows the Databricks SDK refreshes auth tokens
    via ``subprocess.run(["databricks", "auth", "token", ...], shell=True)``
    without setting stdin — the spawned ``databricks.exe`` blocks reading
    from the shared pipe, hanging every MCP tool call.

    Fix: default stdin to DEVNULL so child processes never touch the pipe.

    See: https://github.com/modelcontextprotocol/python-sdk/issues/671
    """
    _original_run = subprocess.run

    @functools.wraps(_original_run)
    def _patched_run(*args, **kwargs):
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        return _original_run(*args, **kwargs)

    subprocess.run = _patched_run

    _OriginalPopen = subprocess.Popen

    class _PatchedPopen(_OriginalPopen):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("stdin", subprocess.DEVNULL)
            super().__init__(*args, **kwargs)

    subprocess.Popen = _PatchedPopen


# Apply subprocess patch early — before any Databricks SDK import (Windows only)
if sys.platform == "win32":
    _patch_subprocess_stdin()

# ---------------------------------------------------------------------------
# Server initialisation
# ---------------------------------------------------------------------------

# Disable FastMCP's built-in task worker on Windows.
# The docket worker uses fakeredis XREADGROUP BLOCK which deadlocks
# the ProactorEventLoop, preventing asyncio.to_thread() callbacks.
# Belt-and-suspenders: pass tasks=False AND override _docket_lifespan,
# because tasks=False alone does not prevent the worker from starting.
_fastmcp_kwargs = {}
if sys.platform == "win32":
    _fastmcp_kwargs["tasks"] = False

mcp = FastMCP("Databricks MCP Server", **_fastmcp_kwargs)

if sys.platform == "win32":

    @asynccontextmanager
    async def _noop_lifespan(*args, **kwargs):
        yield

    if hasattr(mcp, "_docket_lifespan"):
        mcp._docket_lifespan = _noop_lifespan

# Register middleware (see middleware.py for details on each)
mcp.add_middleware(TimeoutHandlingMiddleware())

# NOTE: sync tool functions no longer need a custom asyncio.to_thread() patch
# here (#321). FastMCP >=3.2.4 dispatches every sync @mcp.tool to a worker
# thread by default (FunctionTool's run_in_thread=True default, via
# anyio.to_thread.run_sync) — the same protection this project used to patch
# in by hand: it prevents the Windows ProactorEventLoop deadlock and the
# cancellation race ("Request already responded to") that blocking sync tools
# used to cause. Verified against fastmcp 3.4.2. See
# tests/test_windows_compat.py for the regression test.

# Import and register all tools (side-effect imports: each module registers @mcp.tool decorators)
from .tools import (  # noqa: F401, E402
    sql,
    compute,
    file,
    pipelines,
    jobs,
    agent_bricks,
    aibi_dashboards,
    serving,
    unity_catalog,
    volume_files,
    genie,
    manifest,
    vector_search,
    lakebase,
    user,
    apps,
    workspace,
    pdf,
)
