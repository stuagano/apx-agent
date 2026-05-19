"""Tests for apx_agent.evaluate — the Mosaic AI Agent Evaluation wrapper.

Covers:
  1. _extract_messages tolerates the various eval-dataset shapes
     (bare string, request/input/prompt/query/question, full messages).
  2. _extract_response_text pulls the last assistant message content.
  3. evaluate() compiles the agent, builds a predict_fn that returns the
     assistant text, forwards scorers + kwargs to mlflow.genai.evaluate,
     and threads user_token / workspace_host through custom_inputs.
  4. Default scorers are used when none are supplied; an explicit empty
     list is preserved.

mlflow.genai.evaluate is mocked so tests run without a workspace.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("mlflow")

from apx_agent import Agent, evaluate  # noqa: E402
from apx_agent._eval import (  # noqa: E402
    _extract_messages,
    _extract_response_text,
)


# ---------------------------------------------------------------------------
# _extract_messages
# ---------------------------------------------------------------------------


def test_extract_messages_bare_string() -> None:
    assert _extract_messages("hello") == [{"role": "user", "content": "hello"}]


@pytest.mark.parametrize(
    "key", ["request", "input", "prompt", "query", "question"],
)
def test_extract_messages_recognises_common_eval_keys(key: str) -> None:
    out = _extract_messages({key: "what's the lineage?"})
    assert out == [{"role": "user", "content": "what's the lineage?"}]


def test_extract_messages_passes_through_messages_list() -> None:
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ]
    assert _extract_messages({"messages": msgs}) == msgs


def test_extract_messages_falls_back_to_str() -> None:
    out = _extract_messages({"unknown_key": "x"})
    assert out[0]["role"] == "user"
    assert "unknown_key" in out[0]["content"]


# ---------------------------------------------------------------------------
# _extract_response_text
# ---------------------------------------------------------------------------


def test_extract_response_returns_last_assistant_content() -> None:
    response = SimpleNamespace(messages=[
        SimpleNamespace(role="user", content="hi"),
        SimpleNamespace(role="assistant", content="hello there"),
    ])
    assert _extract_response_text(response) == "hello there"


def test_extract_response_walks_from_end() -> None:
    response = SimpleNamespace(messages=[
        SimpleNamespace(role="assistant", content="intermediate"),
        SimpleNamespace(role="tool", content="tool result"),
        SimpleNamespace(role="assistant", content="final"),
    ])
    assert _extract_response_text(response) == "final"


def test_extract_response_empty_when_no_assistant() -> None:
    response = SimpleNamespace(messages=[
        SimpleNamespace(role="user", content="x"),
    ])
    assert _extract_response_text(response) == ""


def test_extract_response_handles_no_messages() -> None:
    assert _extract_response_text(SimpleNamespace(messages=[])) == ""
    assert _extract_response_text(SimpleNamespace()) == ""


# ---------------------------------------------------------------------------
# evaluate() — happy path
# ---------------------------------------------------------------------------


def _trivial_tool(query: str) -> str:
    """Trivial tool with no dependencies."""
    return f"echo: {query}"


def _fake_chat_agent_with_response(text: str) -> Any:
    """Fake ApxChatAgent — predict() returns a response whose last assistant
    message has content=text."""
    chat = MagicMock(name="ApxChatAgent")

    def _predict(messages: list[Any], custom_inputs: dict[str, Any] | None = None) -> Any:
        chat._last_messages = messages
        chat._last_custom_inputs = custom_inputs
        return SimpleNamespace(messages=[
            *messages,
            SimpleNamespace(role="assistant", content=text),
        ])

    chat.predict = MagicMock(side_effect=_predict)
    return chat


def test_evaluate_compiles_and_invokes_predict_fn() -> None:
    agent = Agent(tools=[_trivial_tool])
    fake_chat = _fake_chat_agent_with_response("the lineage is upstream.gold.orders")

    fake_eval = MagicMock(return_value=SimpleNamespace(metrics={"correctness": 0.9}))

    with patch("apx_agent._eval.compile_to_chat_agent", return_value=fake_chat), \
         patch("mlflow.genai.evaluate", fake_eval):
        result = evaluate(
            agent,
            model="databricks-claude-sonnet-4-6",
            evalset=[{"request": "what is the lineage?"}],
            scorers=[],
        )

    # mlflow.genai.evaluate was called once with our predict_fn
    fake_eval.assert_called_once()
    kwargs = fake_eval.call_args.kwargs
    assert kwargs["data"] == [{"request": "what is the lineage?"}]
    assert kwargs["scorers"] == []
    predict_fn = kwargs["predict_fn"]

    # Invoke the predict_fn the way mlflow would
    out = predict_fn({"request": "what is the lineage?"})
    assert out == "the lineage is upstream.gold.orders"

    # The compiled chat agent received exactly one user message with our
    # request as content
    assert len(fake_chat._last_messages) == 1
    assert fake_chat._last_messages[0].content == "what is the lineage?"


def test_evaluate_threads_user_token_through_custom_inputs() -> None:
    agent = Agent(tools=[_trivial_tool])
    fake_chat = _fake_chat_agent_with_response("ok")
    fake_eval = MagicMock(return_value=None)

    with patch("apx_agent._eval.compile_to_chat_agent", return_value=fake_chat), \
         patch("mlflow.genai.evaluate", fake_eval):
        evaluate(
            agent,
            model="m",
            evalset=[],
            scorers=[],
            user_token="user-obo-token-xyz",
            workspace_host="https://my-workspace.cloud.databricks.com",
        )

    predict_fn = fake_eval.call_args.kwargs["predict_fn"]
    predict_fn("hi")

    assert fake_chat._last_custom_inputs == {
        "user_token": "user-obo-token-xyz",
        "workspace_host": "https://my-workspace.cloud.databricks.com",
    }


def test_evaluate_no_user_token_means_no_custom_inputs() -> None:
    agent = Agent(tools=[_trivial_tool])
    fake_chat = _fake_chat_agent_with_response("ok")
    fake_eval = MagicMock()

    with patch("apx_agent._eval.compile_to_chat_agent", return_value=fake_chat), \
         patch("mlflow.genai.evaluate", fake_eval):
        evaluate(agent, model="m", evalset=[], scorers=[])

    predict_fn = fake_eval.call_args.kwargs["predict_fn"]
    predict_fn("hi")
    assert fake_chat._last_custom_inputs is None


def test_evaluate_forwards_mlflow_kwargs() -> None:
    agent = Agent(tools=[_trivial_tool])
    fake_chat = _fake_chat_agent_with_response("ok")
    fake_eval = MagicMock()

    with patch("apx_agent._eval.compile_to_chat_agent", return_value=fake_chat), \
         patch("mlflow.genai.evaluate", fake_eval):
        evaluate(
            agent,
            model="m",
            evalset=[],
            scorers=[],
            experiment_id="exp-123",
            model_id="model-abc",
        )

    kwargs = fake_eval.call_args.kwargs
    assert kwargs["experiment_id"] == "exp-123"
    assert kwargs["model_id"] == "model-abc"


def test_evaluate_uses_default_scorers_when_none() -> None:
    agent = Agent(tools=[_trivial_tool])
    fake_chat = _fake_chat_agent_with_response("ok")
    fake_eval = MagicMock()
    sentinel_scorers = ["correctness_sentinel", "relevance_sentinel"]

    with patch("apx_agent._eval.compile_to_chat_agent", return_value=fake_chat), \
         patch("apx_agent._eval._default_scorers", return_value=sentinel_scorers), \
         patch("mlflow.genai.evaluate", fake_eval):
        evaluate(agent, model="m", evalset=[])  # scorers omitted

    assert fake_eval.call_args.kwargs["scorers"] == sentinel_scorers


def test_evaluate_preserves_explicit_empty_scorers_list() -> None:
    """Explicit scorers=[] means "no scorers" — don't substitute defaults."""
    agent = Agent(tools=[_trivial_tool])
    fake_chat = _fake_chat_agent_with_response("ok")
    fake_eval = MagicMock()

    with patch("apx_agent._eval.compile_to_chat_agent", return_value=fake_chat), \
         patch("apx_agent._eval._default_scorers", return_value=["should_not_be_used"]), \
         patch("mlflow.genai.evaluate", fake_eval):
        evaluate(agent, model="m", evalset=[], scorers=[])

    assert fake_eval.call_args.kwargs["scorers"] == []


# ---------------------------------------------------------------------------
# experiment kwarg
# ---------------------------------------------------------------------------


def test_evaluate_sets_experiment_when_provided() -> None:
    agent = Agent(tools=[_trivial_tool])
    fake_chat = _fake_chat_agent_with_response("ok")
    fake_eval = MagicMock()

    with patch("apx_agent._eval.compile_to_chat_agent", return_value=fake_chat), \
         patch("mlflow.set_experiment") as mock_set, \
         patch("mlflow.genai.evaluate", fake_eval):
        evaluate(
            agent,
            model="m",
            evalset=[],
            scorers=[],
            experiment="/Users/me/agents/triage",
        )

    mock_set.assert_called_once_with("/Users/me/agents/triage")


def test_evaluate_skips_set_experiment_when_omitted() -> None:
    agent = Agent(tools=[_trivial_tool])
    fake_chat = _fake_chat_agent_with_response("ok")
    fake_eval = MagicMock()

    with patch("apx_agent._eval.compile_to_chat_agent", return_value=fake_chat), \
         patch("mlflow.set_experiment") as mock_set, \
         patch("mlflow.genai.evaluate", fake_eval):
        evaluate(agent, model="m", evalset=[], scorers=[])

    mock_set.assert_not_called()


def test_evaluate_friendly_error_when_set_experiment_fails() -> None:
    agent = Agent(tools=[_trivial_tool])

    with patch("mlflow.set_experiment", side_effect=RuntimeError("bad path")):
        with pytest.raises(RuntimeError, match="mlflow.set_experiment"):
            evaluate(
                agent,
                model="m",
                evalset=[],
                scorers=[],
                experiment="/bad/path",
            )
