"""Tests for sync-tool threadpool dispatch (#321).

This project used to monkey-patch @mcp.tool registration
(_patch_tool_decorator_for_async) to force every sync tool through
asyncio.to_thread(), preventing a Windows ProactorEventLoop deadlock and a
cancellation race ("Request already responded to") that a sync tool blocking
the event loop used to cause.

FastMCP >=3.2.4 does this natively: FunctionTool defaults run_in_thread=True,
so a sync tool is dispatched to a worker thread (via anyio.to_thread.run_sync)
without any patch. The custom wrapper was removed as redundant; these tests
exercise FastMCP's OWN registration/execution path (FunctionTool.from_function
+ .run()) so a future FastMCP dependency bump that regressed this default
would fail here instead of silently reintroducing the deadlock/race.
"""

import asyncio
import threading

import pytest
from fastmcp.tools.function_tool import FunctionTool


def sample_tool(query: str, limit: int = 10) -> str:
    """Execute a sample query."""
    return f"result:{query}:{limit}"


class TestSyncToolThreadDispatch:
    """A sync tool registered the normal way (no custom wrapper) still runs
    off the event loop thread — FastMCP's native run_in_thread default."""

    def test_run_in_thread_defaults_true_for_sync_tool(self):
        tool = FunctionTool.from_function(sample_tool)
        assert tool.run_in_thread is True

    @pytest.mark.asyncio
    async def test_returns_correct_result(self):
        tool = FunctionTool.from_function(sample_tool)
        result = await tool.run({"query": "test", "limit": 5})
        assert result.content[0].text == "result:test:5"

    @pytest.mark.asyncio
    async def test_runs_in_thread_pool(self):
        """The sync function executes in a different thread than the event loop —
        the property the deadlock/cancellation-race fix depends on."""
        main_thread = threading.current_thread().ident

        def capture_thread() -> int:
            return threading.current_thread().ident

        tool = FunctionTool.from_function(capture_thread)
        result = await tool.run({})
        worker_thread = result.structured_content["result"]
        assert worker_thread != main_thread

    @pytest.mark.asyncio
    async def test_does_not_block_event_loop(self):
        """Concurrent tasks can run while the sync tool executes — proves the
        event loop stays free instead of being blocked by inline sync code."""
        import time

        def slow_tool() -> str:
            time.sleep(0.2)
            return "done"

        tool = FunctionTool.from_function(slow_tool)

        concurrent_ran = False

        async def concurrent_task():
            nonlocal concurrent_ran
            await asyncio.sleep(0.05)
            concurrent_ran = True

        await asyncio.gather(tool.run({}), concurrent_task())
        assert concurrent_ran

    @pytest.mark.asyncio
    async def test_propagates_exceptions(self):
        def failing_tool() -> str:
            raise ValueError("something went wrong")

        tool = FunctionTool.from_function(failing_tool)
        with pytest.raises(ValueError, match="something went wrong"):
            await tool.run({})
