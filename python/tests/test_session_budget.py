"""session_budget — cumulative per-session token cap on the served paths (#768).

Every AC drives the SERVED path (`chat_agent_for` / `compile_to_responses_agent`
with an InMemorySaver checkpointer + a stable thread_id across turns), never
`run_once`. The cumulative counter lives in the graph `state` channel
(`state["session_tokens"]`), which the checkpointer persists per thread_id.

Each turn recompiles the graph, so `_build_chat_databricks` is patched to hand
back a FRESH fake chat model per compile — every turn emits one AIMessage whose
`usage_metadata` sums to a known per-turn total (default 60 = 30 in + 30 out).
Two 60-token turns cross a 100-token cap; the SECOND turn raises.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk  # noqa: E402
from langchain_core.outputs import (  # noqa: E402
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
)
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from mlflow.types.agent import ChatAgentMessage  # noqa: E402

from apx_agent import (  # noqa: E402
    LlmAgent,
    SessionBudgetExceeded,
    chat_agent_for,
    compile_to_responses_agent,
)
from apx_agent import _compile  # noqa: E402

PER_TURN = 60  # 30 input + 30 output
_USAGE = {"input_tokens": 30, "output_tokens": 30, "total_tokens": PER_TURN}


class _UsageFake(BaseChatModel):
    """Fake chat model that reports ``usage_metadata`` on BOTH invoke and stream.

    ``GenericFakeChatModel`` drops usage_metadata when streamed, which real
    Databricks-Claude does not — it reports usage on the final streamed chunk.
    The responses path streams (``stream_mode=["updates","messages"]``), so the
    fake must carry usage through streaming for the cumulative cap to be
    exercisable on that path (#768 AC-4).
    """

    reply: str

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None,
                  **kwargs: Any) -> ChatResult:
        msg = AIMessage(content=self.reply, usage_metadata=_USAGE)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None,
                **kwargs: Any) -> Any:
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=self.reply, usage_metadata=_USAGE)
        )

    @property
    def _llm_type(self) -> str:
        return "usage-fake"


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every compile gets a fresh model emitting one usage-bearing AIMessage."""
    counter = {"n": 0}

    def _build(endpoint: Any, *, temperature: Any = None, max_tokens: Any = None) -> Any:
        i = counter["n"]
        counter["n"] += 1
        return _UsageFake(reply=f"r{i}")

    monkeypatch.setattr(_compile, "_build_chat_databricks", _build)


def _agent(tokens: int | None = 100) -> LlmAgent:
    sb = {"tokens": tokens} if tokens is not None else None
    return LlmAgent(name="a", tools=[], instructions="Be helpful.", session_budget=sb)


def _ws() -> Any:
    ws = MagicMock(name="ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    return ws


def _predict(chat: Any, text: str, uid: str, session: str) -> Any:
    return chat.predict(
        [ChatAgentMessage(role="user", content=text, id=uid)],
        custom_inputs={"session_id": session},
    )


# --------------------------------------------------------------------------- AC-1
def test_ac1_cumulative_predict_crosses_cap(fake_model: None) -> None:
    chat = chat_agent_for(_agent(100), model="m", conversation_store=None, checkpointer=InMemorySaver())
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        _predict(chat, "one", "u1", "T")  # 60 cumulative — under cap, no raise
        with pytest.raises(SessionBudgetExceeded) as ei:
            _predict(chat, "two", "u2", "T")  # 120 cumulative — crosses
    assert ei.value.spent >= 120 and ei.value.cap == 100


# --------------------------------------------------------------------------- AC-2
def test_ac2_under_cap_no_raise(fake_model: None) -> None:
    chat = chat_agent_for(_agent(1000), model="m", conversation_store=None, checkpointer=InMemorySaver())
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        _predict(chat, "one", "u1", "T")  # 60
        _predict(chat, "two", "u2", "T")  # 120 — still < 1000, no raise


# --------------------------------------------------------------------------- AC-3
def test_ac3_predict_stream_cumulative(fake_model: None) -> None:
    chat = chat_agent_for(_agent(100), model="m", conversation_store=None, checkpointer=InMemorySaver())
    ci = {"session_id": "S"}
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        list(chat.predict_stream([ChatAgentMessage(role="user", content="one", id="u1")], custom_inputs=ci))
        with pytest.raises(SessionBudgetExceeded) as ei:
            list(chat.predict_stream([ChatAgentMessage(role="user", content="two", id="u2")], custom_inputs=ci))
    assert ei.value.spent >= 120 and ei.value.cap == 100


# --------------------------------------------------------------------------- AC-4
def test_ac4_responses_cumulative(fake_model: None) -> None:
    from mlflow.types.responses import ResponsesAgentRequest

    def _req(text: str) -> Any:
        return ResponsesAgentRequest(
            input=[{"role": "user", "content": text}], custom_inputs={"thread_id": "T"}
        )

    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        non_streaming, streaming = compile_to_responses_agent(
            _agent(100), model="m", checkpointer=InMemorySaver()
        )
        non_streaming(_req("one"))  # 60
        with pytest.raises(SessionBudgetExceeded) as ei:
            non_streaming(_req("two"))  # 120
    assert ei.value.spent >= 120 and ei.value.cap == 100

    # streaming entrypoint, independent thread
    def _req_s(text: str) -> Any:
        return ResponsesAgentRequest(
            input=[{"role": "user", "content": text}], custom_inputs={"thread_id": "S"}
        )

    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        _, streaming = compile_to_responses_agent(_agent(100), model="m", checkpointer=InMemorySaver())
        list(streaming(_req_s("one")))
        with pytest.raises(SessionBudgetExceeded) as ei2:
            list(streaming(_req_s("two")))
    assert ei2.value.spent >= 120 and ei2.value.cap == 100


# --------------------------------------------------------------------------- AC-5
def test_ac5_over_at_turn_start_refuses(fake_model: None) -> None:
    saver = InMemorySaver()
    chat = chat_agent_for(_agent(100), model="m", conversation_store=None, checkpointer=saver)
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        _predict(chat, "one", "u1", "T")  # 60
        with pytest.raises(SessionBudgetExceeded):
            _predict(chat, "two", "u2", "T")  # 120 → persists over-cap total
        # Now the session is already over cap; the next turn must refuse BEFORE running.
        with pytest.raises(SessionBudgetExceeded) as ei:
            _predict(chat, "three", "u3", "T")
    assert ei.value.spent >= 100 and ei.value.cap == 100


# --------------------------------------------------------------------------- AC-6
def test_ac6_no_cross_session_leak(fake_model: None) -> None:
    chat = chat_agent_for(_agent(100), model="m", conversation_store=None, checkpointer=InMemorySaver())
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        _predict(chat, "a1", "u1", "A")  # session A: 60
        _predict(chat, "b1", "u2", "B")  # session B: 60 (independent, no raise)
        with pytest.raises(SessionBudgetExceeded):
            _predict(chat, "b2", "u3", "B")  # session B: 120 → B crosses
        # A only ran once (60); B crossing must NOT have leaked into A. A's
        # second turn is what pushes A to 120 and crosses — proving independence.
        with pytest.raises(SessionBudgetExceeded) as ei:
            _predict(chat, "a2", "u4", "A")  # session A: 120
    assert ei.value.spent >= 120 and ei.value.cap == 100


# --------------------------------------------------------------------------- AC-7
def test_ac7_exception_shape_and_validation() -> None:
    exc = SessionBudgetExceeded(spent=120, cap=100)
    assert isinstance(exc.spent, int) and isinstance(exc.cap, int)
    assert exc.spent == 120 and exc.cap == 100
    assert SessionBudgetExceeded.__doc__ is not None
    doc = SessionBudgetExceeded.__doc__.lower()
    assert "per-session" in doc or "cumulative" in doc
    assert "turn-boundary" in doc or "turn boundary" in doc
    assert "same iteration" not in doc
    # tokens-only validation
    with pytest.raises(ValueError):
        LlmAgent(name="a", tools=[], instructions="x", session_budget={"dollars": 5})


# --------------------------------------------------------------------------- AC-8
def test_ac8_served_path_marker(fake_model: None) -> None:
    """The enforcement fires through the SERVED ApxChatAgent.predict, not run_once."""
    from mlflow.pyfunc import ChatAgent

    chat = chat_agent_for(_agent(100), model="m", conversation_store=None, checkpointer=InMemorySaver())
    assert isinstance(chat, ChatAgent)  # served MLflow ChatAgent, not run_once
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        _predict(chat, "one", "u1", "T")
        with pytest.raises(SessionBudgetExceeded):
            _predict(chat, "two", "u2", "T")


# --------------------------------------------------------------------------- AC-9
def test_ac9_suite_regression_marker() -> None:
    """Marker: the feature adds no new imports/paths that break the suite.
    Real regression proof is `make check`; this just anchors the AC id."""
    import apx_agent

    assert hasattr(apx_agent, "SessionBudgetExceeded")
