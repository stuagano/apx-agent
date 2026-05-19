"""Tests for ``run_once`` — batch/scheduled-job invocation primitive.

Covers:
  - Compiles + invokes the agent with the explicit ``model=`` kwarg.
  - Falls back to ``[tool.apx.agent].model`` in pyproject when ``model`` is None.
  - Raises ``ValueError`` when no model resolvable from any source.
  - Returns the final assistant message text from ChatAgentResponse.
  - Returns "" when no assistant message is present.
  - Forwards ``session_id`` into ``custom_inputs`` for the predict call.
  - Merges caller-supplied ``custom_inputs`` with the session_id.
  - ``custom_inputs=None`` when neither session_id nor custom_inputs given.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


pytest.importorskip("mlflow")


from apx_agent import LlmAgent, run_once


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(text: str | None = "ok", role: str = "assistant"):
    """Build a fake ChatAgentResponse with one message."""
    import uuid
    from mlflow.types.agent import ChatAgentMessage, ChatAgentResponse
    return ChatAgentResponse(
        messages=[ChatAgentMessage(id=str(uuid.uuid4()), role=role, content=text or "")]
    )


def _make_empty_response():
    from mlflow.types.agent import ChatAgentResponse
    return ChatAgentResponse(messages=[])


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def test_uses_explicit_model_kwarg() -> None:
    agent = LlmAgent(tools=[])
    fake_chat_agent = MagicMock()
    fake_chat_agent.predict.return_value = _make_response("hello")

    with patch("apx_agent._run_once.compile_to_chat_agent") as compile_mock:
        compile_mock.return_value = fake_chat_agent
        run_once(agent, "hi", model="databricks-claude-sonnet-4-6")

    compile_mock.assert_called_once_with(agent, model="databricks-claude-sonnet-4-6")


def test_falls_back_to_pyproject_model_when_kwarg_missing() -> None:
    agent = LlmAgent(tools=[])
    fake_chat_agent = MagicMock()
    fake_chat_agent.predict.return_value = _make_response("hello")
    fake_cfg = MagicMock(model="databricks-meta-llama-3-3-70b-instruct")

    with patch("apx_agent._run_once.compile_to_chat_agent") as compile_mock, \
         patch("apx_agent._run_once._load_agent_config", return_value=fake_cfg):
        compile_mock.return_value = fake_chat_agent
        run_once(agent, "hi")

    compile_mock.assert_called_once_with(agent, model="databricks-meta-llama-3-3-70b-instruct")


def test_raises_when_no_model_resolvable() -> None:
    agent = LlmAgent(tools=[])
    with patch("apx_agent._run_once._load_agent_config", return_value=None), \
         pytest.raises(ValueError, match="run_once requires a model"):
        run_once(agent, "hi")


def test_raises_when_pyproject_config_has_empty_model() -> None:
    agent = LlmAgent(tools=[])
    fake_cfg = MagicMock(model="")
    with patch("apx_agent._run_once._load_agent_config", return_value=fake_cfg), \
         pytest.raises(ValueError, match="run_once requires a model"):
        run_once(agent, "hi")


# ---------------------------------------------------------------------------
# Return value extraction
# ---------------------------------------------------------------------------


def test_returns_final_assistant_text() -> None:
    agent = LlmAgent(tools=[])
    fake_chat_agent = MagicMock()
    fake_chat_agent.predict.return_value = _make_response("final answer text")

    with patch("apx_agent._run_once.compile_to_chat_agent", return_value=fake_chat_agent):
        result = run_once(agent, "hi", model="m")

    assert result == "final answer text"


def test_returns_empty_string_when_no_messages() -> None:
    agent = LlmAgent(tools=[])
    fake_chat_agent = MagicMock()
    fake_chat_agent.predict.return_value = _make_empty_response()

    with patch("apx_agent._run_once.compile_to_chat_agent", return_value=fake_chat_agent):
        result = run_once(agent, "hi", model="m")

    assert result == ""


def test_returns_empty_string_when_no_assistant_message() -> None:
    """A response with only system/user/tool messages should return ''."""
    import uuid
    from mlflow.types.agent import ChatAgentMessage, ChatAgentResponse

    agent = LlmAgent(tools=[])
    fake_chat_agent = MagicMock()
    fake_chat_agent.predict.return_value = ChatAgentResponse(
        messages=[ChatAgentMessage(id=str(uuid.uuid4()), role="user", content="user said something")],
    )

    with patch("apx_agent._run_once.compile_to_chat_agent", return_value=fake_chat_agent):
        result = run_once(agent, "hi", model="m")

    assert result == ""


def test_extracts_last_assistant_when_multiple_present() -> None:
    """Tool-calling loops produce multiple assistant messages; take the last."""
    import uuid
    from mlflow.types.agent import ChatAgentMessage, ChatAgentResponse

    agent = LlmAgent(tools=[])
    fake_chat_agent = MagicMock()
    fake_chat_agent.predict.return_value = ChatAgentResponse(
        messages=[
            ChatAgentMessage(id=str(uuid.uuid4()), role="assistant", content="step 1: calling tool..."),
            ChatAgentMessage(id=str(uuid.uuid4()), role="tool", content="{'rows': []}", tool_call_id="tc1", name="some_tool"),
            ChatAgentMessage(id=str(uuid.uuid4()), role="assistant", content="step 2: final synthesis"),
        ],
    )

    with patch("apx_agent._run_once.compile_to_chat_agent", return_value=fake_chat_agent):
        result = run_once(agent, "hi", model="m")

    assert result == "step 2: final synthesis"


# ---------------------------------------------------------------------------
# session_id + custom_inputs plumbing
# ---------------------------------------------------------------------------


def test_no_custom_inputs_passed_when_unset() -> None:
    agent = LlmAgent(tools=[])
    fake_chat_agent = MagicMock()
    fake_chat_agent.predict.return_value = _make_response()

    with patch("apx_agent._run_once.compile_to_chat_agent", return_value=fake_chat_agent):
        run_once(agent, "hi", model="m")

    call_kwargs = fake_chat_agent.predict.call_args.kwargs
    assert call_kwargs["custom_inputs"] is None


def test_session_id_forwarded_into_custom_inputs() -> None:
    agent = LlmAgent(tools=[])
    fake_chat_agent = MagicMock()
    fake_chat_agent.predict.return_value = _make_response()

    with patch("apx_agent._run_once.compile_to_chat_agent", return_value=fake_chat_agent):
        run_once(agent, "hi", model="m", session_id="sess-123")

    call_kwargs = fake_chat_agent.predict.call_args.kwargs
    assert call_kwargs["custom_inputs"] == {"session_id": "sess-123"}


def test_custom_inputs_merged_with_session_id() -> None:
    agent = LlmAgent(tools=[])
    fake_chat_agent = MagicMock()
    fake_chat_agent.predict.return_value = _make_response()

    with patch("apx_agent._run_once.compile_to_chat_agent", return_value=fake_chat_agent):
        run_once(
            agent,
            "hi",
            model="m",
            session_id="sess-123",
            custom_inputs={"user_token": "abc"},
        )

    call_kwargs = fake_chat_agent.predict.call_args.kwargs
    assert call_kwargs["custom_inputs"] == {
        "session_id": "sess-123",
        "user_token": "abc",
    }


def test_explicit_session_id_in_custom_inputs_wins() -> None:
    """If caller passes session_id IN custom_inputs, don't clobber it with the kwarg."""
    agent = LlmAgent(tools=[])
    fake_chat_agent = MagicMock()
    fake_chat_agent.predict.return_value = _make_response()

    with patch("apx_agent._run_once.compile_to_chat_agent", return_value=fake_chat_agent):
        run_once(
            agent,
            "hi",
            model="m",
            session_id="from-kwarg",
            custom_inputs={"session_id": "from-custom"},
        )

    call_kwargs = fake_chat_agent.predict.call_args.kwargs
    assert call_kwargs["custom_inputs"]["session_id"] == "from-custom"


def test_prompt_becomes_a_user_message() -> None:
    agent = LlmAgent(tools=[])
    fake_chat_agent = MagicMock()
    fake_chat_agent.predict.return_value = _make_response()

    with patch("apx_agent._run_once.compile_to_chat_agent", return_value=fake_chat_agent):
        run_once(agent, "the actual prompt", model="m")

    messages = fake_chat_agent.predict.call_args.kwargs["messages"]
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert messages[0].content == "the actual prompt"
