"""deferred tool_loading — tool_search router on the compiled served path (#767).

Every runtime AC drives ``chat_agent_for`` (MLflow ChatAgent), never ``run_once``.
The first model hop on a deferred agent must advertise only ``tool_search``;
matched author tools bind for later hops and actually execute. Eager (default)
still binds the full inventory and never injects ``tool_search``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage  # noqa: E402
from mlflow.types.agent import ChatAgentMessage  # noqa: E402

from apx_agent import LlmAgent, chat_agent_for  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._tool_search import (  # noqa: E402
    TOOL_SEARCH_NAME,
    rank_tools,
)


CALLS: list[str] = []


def lookup_order(order_id: str) -> str:
    """Look up a customer order by id."""
    CALLS.append(f"lookup_order:{order_id}")
    return f"order {order_id} found"


def refund_order(order_id: str) -> str:
    """Refund a previously looked-up order."""
    CALLS.append(f"refund_order:{order_id}")
    return f"refunded {order_id}"


def list_inventory() -> str:
    """List warehouse inventory."""
    CALLS.append("list_inventory")
    return "sku-1 in stock"


BOUND: list[list[str]] = []


class _RecordingFake(GenericFakeChatModel):
    """Scripted model that records the tool names bound on each hop.

    GenericFakeChatModel is a Pydantic model — extra instance attrs are
    rejected, so hops go on the module-level ``BOUND`` list.
    """

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        names: list[str] = []
        for tool in tools:
            if hasattr(tool, "name") and tool.name:
                names.append(tool.name)
            elif isinstance(tool, dict):
                fn = tool.get("function")
                nested = fn.get("name") if isinstance(fn, dict) else None
                label = tool.get("name") or nested
                if label:
                    names.append(label)
        BOUND.append(names)
        return self


def _ws() -> Any:
    ws = MagicMock(name="ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    return ws


def _deferred() -> LlmAgent:
    return LlmAgent(
        name="a",
        tools=[lookup_order, refund_order, list_inventory],
        instructions="Use tools when needed.",
        tool_loading="deferred",
    )


def _eager() -> LlmAgent:
    return LlmAgent(
        name="a",
        tools=[lookup_order, refund_order, list_inventory],
        instructions="Use tools when needed.",
    )


def _install(monkeypatch: pytest.MonkeyPatch, messages: list[Any]) -> _RecordingFake:
    model = _RecordingFake(messages=iter(messages))
    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: model,
    )
    return model


def _search_call(query: str, call_id: str = "s1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": TOOL_SEARCH_NAME,
            "args": {"query": query},
            "id": call_id,
        }],
    )


def _lookup_call(order_id: str = "42", call_id: str = "t1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "lookup_order",
            "args": {"order_id": order_id},
            "id": call_id,
        }],
    )


def _predict(chat: Any, text: str, uid: str = "u1") -> Any:
    return chat.predict(
        [ChatAgentMessage(role="user", content=text, id=uid)],
        custom_inputs={"session_id": "T"},
    )


def _contents(resp: Any) -> str:
    return " ".join(m.content for m in resp.messages if m.content)


# --------------------------------------------------------------------------- AC-1
def test_ac1_first_hop_advertises_only_tool_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    CALLS.clear()
    BOUND.clear()
    model = _install(monkeypatch, [_search_call("lookup"), AIMessage(content="ok")])
    chat = chat_agent_for(_deferred(), model="m", conversation_store=None)
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        _predict(chat, "find an order")
    assert BOUND, "create_agent never bound tools"
    first = set(BOUND[0])
    assert first == {TOOL_SEARCH_NAME}
    assert not first & {"lookup_order", "refund_order", "list_inventory"}


# --------------------------------------------------------------------------- AC-2
def test_ac2_matched_tool_actually_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    CALLS.clear()
    BOUND.clear()
    model = _install(
        monkeypatch,
        [_search_call("lookup"), _lookup_call("42"), AIMessage(content="order 42 found")],
    )
    chat = chat_agent_for(_deferred(), model="m", conversation_store=None)
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        resp = _predict(chat, "lookup order 42")
    assert "lookup_order:42" in CALLS
    assert "refund_order:42" not in CALLS
    assert "order 42 found" in _contents(resp)
    # After search, the real tool is advertised to the model.
    assert any("lookup_order" in hop for hop in BOUND[1:])


# --------------------------------------------------------------------------- AC-3
def test_ac3_unmatched_query_keeps_real_tools_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    CALLS.clear()
    BOUND.clear()
    model = _install(
        monkeypatch,
        [_search_call("zzzz-no-match"), AIMessage(content="nothing matched")],
    )
    chat = chat_agent_for(_deferred(), model="m", conversation_store=None)
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        _predict(chat, "do something unknown")
    assert CALLS == []
    # Every hop still sees only tool_search — unmatched names stay hidden.
    for hop in BOUND:
        names = set(hop)
        assert TOOL_SEARCH_NAME in names
        assert not names & {"lookup_order", "refund_order", "list_inventory"}


# --------------------------------------------------------------------------- AC-4
def test_ac4_eager_binds_all_real_tools_and_no_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    CALLS.clear()
    BOUND.clear()
    model = _install(monkeypatch, [AIMessage(content="plain")])
    chat = chat_agent_for(_eager(), model="m", conversation_store=None)
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        _predict(chat, "hello")
    assert BOUND, "eager path never bound tools"
    first = set(BOUND[0])
    assert TOOL_SEARCH_NAME not in first
    assert {"lookup_order", "refund_order", "list_inventory"} <= first


# --------------------------------------------------------------------------- AC-5
def test_ac5_bad_tool_loading_raises_at_construct() -> None:
    for bad in ("lazy", True, False, "", "EAGER"):
        with pytest.raises(ValueError, match="tool_loading"):
            LlmAgent(name="a", tools=[], tool_loading=bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- AC-6
def test_ac6_served_path_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    from mlflow.pyfunc import ChatAgent

    CALLS.clear()
    BOUND.clear()
    _install(monkeypatch, [_search_call("lookup"), AIMessage(content="ok")])
    chat = chat_agent_for(_deferred(), model="m", conversation_store=None)
    assert isinstance(chat, ChatAgent)
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        _predict(chat, "find an order")


def test_rank_tools_substring_not_bm25() -> None:
    catalog = [
        {"name": "lookup_order", "description": "Look up a customer order by id."},
        {"name": "refund_order", "description": "Refund a previously looked-up order."},
        {"name": "list_inventory", "description": "List warehouse inventory."},
    ]
    names = [entry["name"] for entry in rank_tools("lookup", catalog)]
    assert names == ["lookup_order"]
    assert rank_tools("zzzz-no-match", catalog) == []
    assert rank_tools("", catalog) == []
