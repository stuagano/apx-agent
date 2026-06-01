"""Enforcement tests for before_tool guard propagation through the real execution path.

Security property under test
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
When a ``before_tool`` guard raises ``PermissionError``, the denied tool's
*body* must never execute.  The fix is ``raise_error = True`` on
``_AgentCallbackHandler`` — without it, LangChain's ``handle_event`` /
``ahandle_event`` logs the exception and swallows it, so the tool body runs
as if the guard never existed.

Execution layer used
~~~~~~~~~~~~~~~~~~~~
These tests drive the denied tool through the **real ToolNode / compiled
StateGraph path** that production uses (``create_agent`` wraps its tools in
a ToolNode).  A single ToolNode is compiled into a minimal two-node
StateGraph (START → tools → END); the input is an ``AIMessage`` with
``tool_calls``, which bypasses the model node.  This is the minimal offline
reproducer that accurately reflects the production call-stack:

    compiled.invoke/ainvoke
      → Pregel._step
        → ToolNode._func / _afunc
          → _execute_tool_sync / _execute_tool_async
            → tool.invoke(call_args, config)
              → tool.run(…, config)
                → CallbackManager.on_tool_start   ← our handler fires here
                  → handle_event / ahandle_event  ← raise_error controls re-raise
                [body runs here only if exception was NOT raised]

Fail-mode finding (LOCKED)
~~~~~~~~~~~~~~~~~~~~~~~~~~
With ``raise_error = True``:
- ``handle_event`` re-raises the ``PermissionError`` from ``on_tool_start``
  BEFORE the tool body executes.
- The exception propagates through ``BaseTool.run``, ``BaseTool.invoke``,
  ``_execute_tool_sync``, and into ToolNode's ``_default_handle_tool_errors``.
- ``_default_handle_tool_errors`` re-raises for non-``ToolInvocationError``
  exceptions (``PermissionError`` is not a ``ToolInvocationError``), so
  ``_execute_tool_sync`` does NOT swallow it into a ToolMessage.
- The compiled graph therefore raises ``PermissionError`` (FAIL-CLOSED).
- This is the strictest possible outcome: the run aborts, the model does
  NOT see a ToolMessage, and the denied tool's body never executed.

Both sync (``invoke``) and async (``ainvoke``) are tested because the
production ``predict()`` path is async.

Regression tests
~~~~~~~~~~~~~~~~
1. A PERMITTED tool's body still executes through the same graph (fix does
   not break normal operation).
2. An LlmAgent constructed with ``before_tool=ToolDenylist([...])`` also
   enforces the guard (code-defined path, not just raw handler).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool as lc_tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from apx_agent import LlmAgent, ToolDenylist
from apx_agent._callbacks import _AgentCallbackHandler, build_callback_handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_graph(tool_fn: Any) -> Any:
    """Compile a minimal ToolNode graph for offline testing.

    START → tools (ToolNode([tool_fn])) → END
    """
    g = StateGraph(MessagesState)
    g.add_node("tools", ToolNode([tool_fn]))
    g.add_edge(START, "tools")
    g.add_edge("tools", END)
    return g.compile()


def _tool_call_msg(name: str, args: dict[str, Any], call_id: str = "tc1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
    )


# ---------------------------------------------------------------------------
# Security enforcement — body must NOT run when guard raises
# ---------------------------------------------------------------------------


def test_denied_tool_body_does_not_run_sync() -> None:
    """SYNC: denied tool body must NOT execute when guard raises.

    Fail-mode: FAIL-CLOSED — the compiled graph raises PermissionError.
    ToolNode's _default_handle_tool_errors re-raises for non-ToolInvocationError
    exceptions, so the PermissionError propagates and the run aborts.
    The load-bearing assertion is the ``ran`` sentinel: body never executed.
    """
    ran: dict[str, bool] = {"t": False}

    @lc_tool
    def delete_record(x: str) -> str:  # type: ignore[return]
        """Delete a record."""
        ran["t"] = True
        return "deleted"

    h = _AgentCallbackHandler(
        before_tool=lambda n, a: (_ for _ in ()).throw(
            PermissionError(f"{n} blocked by guard")
        )
    )
    graph = _make_graph(delete_record)

    # Fail-closed: the graph raises PermissionError (does NOT return a ToolMessage).
    with pytest.raises(PermissionError, match="blocked by guard"):
        graph.invoke(
            {"messages": [_tool_call_msg("delete_record", {"x": "1"})]},
            config={"callbacks": [h]},
        )

    # SECURITY ASSERTION: the body never ran.
    assert not ran["t"], "guard was swallowed — denied tool body executed!"


@pytest.mark.asyncio
async def test_denied_tool_body_does_not_run_async() -> None:
    """ASYNC: denied tool body must NOT execute when guard raises.

    Fail-mode: FAIL-CLOSED — same as sync path. Both handle_event and
    ahandle_event honor raise_error; ToolNode's async path propagates the
    PermissionError identically to the sync path.
    """
    ran: dict[str, bool] = {"t": False}

    @lc_tool
    def delete_record_async(x: str) -> str:  # type: ignore[return]
        """Delete a record."""
        ran["t"] = True
        return "deleted"

    h = _AgentCallbackHandler(
        before_tool=lambda n, a: (_ for _ in ()).throw(
            PermissionError(f"{n} blocked by guard")
        )
    )
    graph = _make_graph(delete_record_async)

    with pytest.raises(PermissionError, match="blocked by guard"):
        await graph.ainvoke(
            {"messages": [_tool_call_msg("delete_record_async", {"x": "1"})]},
            config={"callbacks": [h]},
        )

    # SECURITY ASSERTION: the body never ran.
    assert not ran["t"], "guard was swallowed on async path — denied tool body executed!"


# ---------------------------------------------------------------------------
# Regression — permitted tool still runs normally
# ---------------------------------------------------------------------------


def test_permitted_tool_runs_normally_sync() -> None:
    """SYNC: a tool NOT blocked by the guard executes normally.

    Ensures raise_error=True does not break the happy path.
    """
    ran: dict[str, bool] = {"t": False}

    @lc_tool
    def safe_tool(x: str) -> str:
        """A safe tool."""
        ran["t"] = True
        return f"result:{x}"

    # Guard only blocks 'delete_record', not 'safe_tool'
    h = _AgentCallbackHandler(
        before_tool=ToolDenylist(["delete_record"])
    )
    graph = _make_graph(safe_tool)

    out = graph.invoke(
        {"messages": [_tool_call_msg("safe_tool", {"x": "hello"})]},
        config={"callbacks": [h]},
    )

    assert ran["t"], "permitted tool body should have run"
    tool_msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs
    assert tool_msgs[0].status == "success"
    assert "result:hello" in tool_msgs[0].content


@pytest.mark.asyncio
async def test_permitted_tool_runs_normally_async() -> None:
    """ASYNC: a tool NOT blocked by the guard executes normally on async path."""
    ran: dict[str, bool] = {"t": False}

    @lc_tool
    def safe_tool_async(x: str) -> str:
        """A safe tool."""
        ran["t"] = True
        return f"result:{x}"

    h = _AgentCallbackHandler(
        before_tool=ToolDenylist(["delete_record"])
    )
    graph = _make_graph(safe_tool_async)

    out = await graph.ainvoke(
        {"messages": [_tool_call_msg("safe_tool_async", {"x": "hello"})]},
        config={"callbacks": [h]},
    )

    assert ran["t"], "permitted tool body should have run on async path"
    tool_msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs
    assert tool_msgs[0].status == "success"


# ---------------------------------------------------------------------------
# Regression — code-defined before_tool path (LlmAgent + ToolDenylist)
# ---------------------------------------------------------------------------


def test_code_defined_before_tool_via_llmagent_and_tooldenylist() -> None:
    """Guard set on LlmAgent via before_tool=ToolDenylist([...]) is enforced.

    Uses build_callback_handler (the compile-path entry point) to construct
    the handler from an LlmAgent, verifying the end-to-end code-defined path.
    The fix repairs both the raw-handler and the code-defined paths.
    Fail-mode: FAIL-CLOSED (graph raises PermissionError).
    """
    ran: dict[str, bool] = {"t": False}

    @lc_tool
    def danger_tool(x: str) -> str:  # type: ignore[return]
        """A dangerous tool."""
        ran["t"] = True
        return "dangerous result"

    agent = LlmAgent(tools=[], before_tool=ToolDenylist(["danger_tool"]))
    handler = build_callback_handler(agent)
    assert handler is not None, "build_callback_handler should return a handler"

    graph = _make_graph(danger_tool)

    # Fail-closed: graph raises PermissionError.
    with pytest.raises(PermissionError):
        graph.invoke(
            {"messages": [_tool_call_msg("danger_tool", {"x": "1"})]},
            config={"callbacks": [handler]},
        )

    # SECURITY ASSERTION: body never ran.
    assert not ran["t"], (
        "code-defined ToolDenylist guard was swallowed — body executed!"
    )


def test_code_defined_before_tool_allows_permitted_tool() -> None:
    """LlmAgent with ToolDenylist allows tools NOT in the deny list."""
    ran: dict[str, bool] = {"t": False}

    @lc_tool
    def allowed_tool(x: str) -> str:
        """An allowed tool."""
        ran["t"] = True
        return "allowed result"

    agent = LlmAgent(tools=[], before_tool=ToolDenylist(["danger_tool"]))
    handler = build_callback_handler(agent)
    assert handler is not None

    graph = _make_graph(allowed_tool)

    out = graph.invoke(
        {"messages": [_tool_call_msg("allowed_tool", {"x": "1"})]},
        config={"callbacks": [handler]},
    )

    assert ran["t"], "permitted tool should have run via code-defined path"
    tool_msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs
    assert tool_msgs[0].status == "success"
