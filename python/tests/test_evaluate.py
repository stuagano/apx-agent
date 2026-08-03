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

import pandas as pd  # noqa: E402

from apx_agent import Agent, evaluate  # noqa: E402
from apx_agent._eval import (  # noqa: E402
    _count_empty_predictions,
    _default_scorers,
    _extract_messages,
    _extract_response_text,
    _normalize_evalset,
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


def test_extract_response_falls_back_to_tool_output_when_ending_on_tool_call() -> None:
    # Agent ends on a tool call (e.g. LoopAgent finish_loop) with no assistant
    # text — the final result lives in the tool message. Issue #264.
    response = SimpleNamespace(messages=[
        SimpleNamespace(role="user", content="extract this"),
        SimpleNamespace(role="assistant", content=""),  # tool-call-only turn
        SimpleNamespace(role="tool", content='{"claim_id": "ABC12345"}'),
    ])
    assert _extract_response_text(response) == '{"claim_id": "ABC12345"}'


def test_extract_response_prefers_assistant_text_over_tool_fallback() -> None:
    # When the agent did emit text earlier, prefer it over a trailing tool turn.
    response = SimpleNamespace(messages=[
        SimpleNamespace(role="assistant", content='{"claim_id": "REAL"}'),
        SimpleNamespace(role="assistant", content=""),  # later tool-call-only turn
        SimpleNamespace(role="tool", content="finish_loop ack"),
    ])
    assert _extract_response_text(response) == '{"claim_id": "REAL"}'


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
# evalset input nesting (#265)
# ---------------------------------------------------------------------------


def test_evaluate_wraps_single_nested_inputs() -> None:
    """A single-nested row (``{"inputs": {"question": ...}}``) — the form the
    docs and examples show — gets wrapped to the double-nested shape mlflow
    requires for a predict_fn whose param is named ``inputs``."""
    agent = Agent(tools=[_trivial_tool])
    fake_chat = _fake_chat_agent_with_response("ok")
    fake_eval = MagicMock()

    evalset = [
        {
            "inputs": {"question": "what is the lineage?"},
            "expectations": {"expected_facts": ["upstream"]},
        },
    ]

    with patch("apx_agent._eval.compile_to_chat_agent", return_value=fake_chat), \
         patch("mlflow.genai.evaluate", fake_eval):
        evaluate(agent, model="m", evalset=evalset, scorers=[])

    assert fake_eval.call_args.kwargs["data"] == [
        {
            "inputs": {"inputs": {"question": "what is the lineage?"}},
            "expectations": {"expected_facts": ["upstream"]},
        },
    ]


def test_evaluate_leaves_double_nested_inputs_untouched() -> None:
    """An already double-nested row passes through unchanged (idempotent)."""
    agent = Agent(tools=[_trivial_tool])
    fake_chat = _fake_chat_agent_with_response("ok")
    fake_eval = MagicMock()

    evalset = [
        {
            "inputs": {"inputs": {"question": "already wrapped"}},
            "expectations": {"expected_facts": ["x"]},
        },
    ]

    with patch("apx_agent._eval.compile_to_chat_agent", return_value=fake_chat), \
         patch("mlflow.genai.evaluate", fake_eval):
        evaluate(agent, model="m", evalset=evalset, scorers=[])

    assert fake_eval.call_args.kwargs["data"] == evalset


def test_normalize_evalset_leaves_non_list_untouched() -> None:
    """DataFrame / path / str shapes pass through unchanged."""
    assert _normalize_evalset("path/to/dataset.csv") == "path/to/dataset.csv"
    sentinel = SimpleNamespace(kind="dataframe")
    assert _normalize_evalset(sentinel) is sentinel


def test_normalize_evalset_skips_rows_without_inputs_key() -> None:
    """Rows lacking an ``inputs`` key (e.g. bare ``{"request": ...}``) are
    left as-is — the request/input/prompt path stays intact."""
    rows = [{"request": "what is the lineage?"}]
    assert _normalize_evalset(rows) == rows


# ---------------------------------------------------------------------------
# _default_scorers — judge model override
# ---------------------------------------------------------------------------


def test_default_scorers_constructs_without_model() -> None:
    """No judge override → scorers build with their own default judge."""
    scorers = _default_scorers()
    # mlflow's eval extra is installed (importorskip above), so the scorer
    # bundle is non-empty.
    assert len(scorers) == 3


def test_default_scorers_applies_judge_model() -> None:
    """A judge override is threaded into each scorer's ``model``."""
    scorers = _default_scorers(model="databricks")
    assert len(scorers) == 3
    assert all(s.model == "databricks" for s in scorers)


def test_default_scorers_includes_safety() -> None:
    from mlflow.genai.scorers import Safety
    scorers = _default_scorers()
    assert any(isinstance(s, Safety) for s in scorers)


def test_evaluate_threads_judge_model_into_default_scorers() -> None:
    """evaluate(judge_model=...) forwards to _default_scorers when scorers
    aren't passed explicitly."""
    agent = Agent(tools=[_trivial_tool])
    fake_chat = _fake_chat_agent_with_response("ok")
    fake_eval = MagicMock()
    captured: dict[str, Any] = {}

    def _fake_default_scorers(model: str | None = None) -> list[Any]:
        captured["model"] = model
        return ["judged_scorer"]

    with patch("apx_agent._eval.compile_to_chat_agent", return_value=fake_chat), \
         patch("apx_agent._eval._default_scorers", _fake_default_scorers), \
         patch("mlflow.genai.evaluate", fake_eval):
        evaluate(agent, model="m", evalset=[], judge_model="databricks")

    assert captured["model"] == "databricks"
    assert fake_eval.call_args.kwargs["scorers"] == ["judged_scorer"]


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


# ---------------------------------------------------------------------------
# _count_empty_predictions — surface None/empty predictions (issue #267)
# ---------------------------------------------------------------------------


def test_count_empty_predictions_mixed_none_empty_whitespace_and_nan() -> None:
    df = pd.DataFrame(
        {
            "outputs": [
                "real answer",
                None,
                "",
                "   ",
                float("nan"),
                "another real answer",
            ]
        }
    )
    result = SimpleNamespace(result_df=df)
    # None + "" + whitespace + NaN = 4 empty, 2 real.
    assert _count_empty_predictions(result) == 4


@pytest.mark.parametrize("column", ["outputs", "response", "predictions"])
def test_count_empty_predictions_detects_each_candidate_column(column: str) -> None:
    df = pd.DataFrame({column: ["ok", None, "ok"]})
    result = SimpleNamespace(result_df=df)
    assert _count_empty_predictions(result) == 1


def test_count_empty_predictions_returns_none_when_no_output_column() -> None:
    df = pd.DataFrame({"request": ["q1", "q2"], "score": [0.9, 0.8]})
    result = SimpleNamespace(result_df=df)
    assert _count_empty_predictions(result) is None


def test_count_empty_predictions_returns_none_without_result_df() -> None:
    assert _count_empty_predictions(SimpleNamespace()) is None
    assert _count_empty_predictions(SimpleNamespace(result_df=None)) is None


def test_count_empty_predictions_zero_when_all_present() -> None:
    df = pd.DataFrame({"outputs": ["a", "b", "c"]})
    result = SimpleNamespace(result_df=df)
    assert _count_empty_predictions(result) == 0
