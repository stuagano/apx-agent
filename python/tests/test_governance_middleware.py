"""Governance-exception middleware converts PermissionError / ToolCancelled into
error ToolMessages on BOTH the sync and async tool-call paths.

Regression guard for #243: a tool-calling agent invoked via ainvoke/astream
(the LangGraphExecutor path) must not raise NotImplementedError because the
middleware only defined the sync hook.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("langchain")

from langchain_core.messages import ToolMessage

from apx_agent._cancellation import ToolCancelled
from apx_agent._compile import _governance_exception_middleware


def _req() -> SimpleNamespace:
    # The middleware only reads request.tool_call["id"].
    return SimpleNamespace(tool_call={"id": "c1"})


def test_sync_converts_governance_error() -> None:
    mw = _governance_exception_middleware()

    def handler(req) -> ToolMessage:  # noqa: ANN001
        raise PermissionError("denied by policy")

    out = mw.wrap_tool_call(_req(), handler)
    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "denied by policy" in out.content
    assert out.tool_call_id == "c1"


def test_sync_passes_success_through() -> None:
    mw = _governance_exception_middleware()
    ok = ToolMessage(content="ok", tool_call_id="c1")

    def handler(req) -> ToolMessage:  # noqa: ANN001
        return ok

    assert mw.wrap_tool_call(_req(), handler) is ok


@pytest.mark.asyncio
async def test_async_converts_governance_error() -> None:
    mw = _governance_exception_middleware()

    async def handler(req) -> ToolMessage:  # noqa: ANN001
        raise ToolCancelled("mytool", "user cancelled the tool")

    out = await mw.awrap_tool_call(_req(), handler)
    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "user cancelled the tool" in out.content
    assert out.tool_call_id == "c1"


@pytest.mark.asyncio
async def test_async_passes_success_through() -> None:
    mw = _governance_exception_middleware()
    ok = ToolMessage(content="ok", tool_call_id="c1")

    async def handler(req) -> ToolMessage:  # noqa: ANN001
        return ok

    assert await mw.awrap_tool_call(_req(), handler) is ok


@pytest.mark.asyncio
async def test_async_lets_real_bugs_propagate() -> None:
    # Genuine bugs (not governance signals) must still fail loud, not be
    # swallowed into a ToolMessage.
    mw = _governance_exception_middleware()

    async def handler(req) -> ToolMessage:  # noqa: ANN001
        raise KeyError("oops")

    with pytest.raises(KeyError):
        await mw.awrap_tool_call(_req(), handler)
