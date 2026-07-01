"""Reality check (ctk): short-term memory through the ResponsesAgent served path.

Mirrors the ChatAgent proof for the OTHER served contract (/responses, Databricks
Apps). Isolates the checkpointer with `conversation_store=None`, delta-only turn 2,
one process-scoped saver. Also pins the ResponsesAgent-specific hazard: the
non-stream path extracts output by SLICING the graph result, which returns the
FULL thread state under a checkpointer — so turn 2 must return only its new
output, never leaked prior-turn history.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")
_responses = pytest.importorskip("mlflow.types.responses")
ResponsesAgentRequest = _responses.ResponsesAgentRequest

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from apx_agent import LlmAgent  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._compile import compile_to_langgraph  # noqa: E402
from apx_agent._responses_agent import compile_to_responses_agent  # noqa: E402


@pytest.fixture(autouse=True)
def _fake_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    model = GenericFakeChatModel(messages=iter([AIMessage(content=f"r{i}") for i in range(20)]))
    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: model,
    )


def _agent() -> LlmAgent:
    return LlmAgent(name="a", tools=[], instructions="Be helpful.")


def _ws() -> Any:
    ws = MagicMock(name="ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    return ws


def _req(text: str, **ci: Any) -> Any:
    return ResponsesAgentRequest(input=[{"role": "user", "content": text}], custom_inputs=ci or None)


def _thread_humans(agent: LlmAgent, saver: Any, thread_id: str) -> list[str]:
    probe = compile_to_langgraph(agent, ws=_ws(), model="m", checkpointer=saver)
    return [
        str(m.content)
        for m in probe.get_state({"configurable": {"thread_id": thread_id}}).values["messages"]
        if isinstance(m, HumanMessage)
    ]


def test_responses_non_stream_recalls_and_returns_only_new_output() -> None:
    saver = InMemorySaver()
    agent = _agent()
    non_stream, _ = compile_to_responses_agent(agent, model="m", checkpointer=saver)
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        non_stream(_req("my name is Zara", thread_id="T"))
        resp2 = non_stream(_req("what is my name", thread_id="T"))  # delta-only turn 2
        humans = _thread_humans(agent, saver, "T")

    # Persistence: both turns in the thread state (turn 1 carried by the saver).
    assert "my name is Zara" in humans and "what is my name" in humans
    # Slice fix: turn 2 returns ONLY its new output — NOT turn 1's leaked history.
    assert len(resp2.output) == 1
    blob = " ".join(str(item.model_dump()) for item in resp2.output)
    assert "my name is Zara" not in blob


def test_responses_stream_recalls_prior_turn() -> None:
    saver = InMemorySaver()
    agent = _agent()
    _, streaming = compile_to_responses_agent(agent, model="m", checkpointer=saver)
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        list(streaming(_req("I live in Oslo", thread_id="S")))
        list(streaming(_req("where do I live", thread_id="S")))  # delta-only
        humans = _thread_humans(agent, saver, "S")
    assert "I live in Oslo" in humans and "where do I live" in humans


def test_responses_no_thread_id_does_not_crash() -> None:
    """No thread_id → a checkpointer-compiled graph would demand one; fall back
    to a stateless turn, not error."""
    non_stream, _ = compile_to_responses_agent(_agent(), model="m", checkpointer=InMemorySaver())
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        resp = non_stream(_req("hi"))  # no custom_inputs
    assert resp is not None and len(resp.output) == 1


def test_responses_short_term_on_by_default() -> None:
    """No checkpointer wired → auto InMemorySaver for the served LlmAgent; two
    turns run and turn 2 returns only its new output (auto-enable + slice)."""
    non_stream, _ = compile_to_responses_agent(_agent(), model="m")  # nothing wired
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        non_stream(_req("favorite color is teal", thread_id="D"))
        resp2 = non_stream(_req("what is my favorite color", thread_id="D"))
    assert len(resp2.output) == 1
    blob = " ".join(str(item.model_dump()) for item in resp2.output)
    assert "favorite color is teal" not in blob
