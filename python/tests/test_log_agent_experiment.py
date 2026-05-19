"""Tests for log_agent's MLflow experiment wiring.

Covers:
  - experiment kwarg calls mlflow.set_experiment(...) before logging
  - experiment omitted skips set_experiment
  - set_experiment failure raises a friendly RuntimeError
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("mlflow")
pytest.importorskip("langgraph")

from apx_agent import Agent, log_agent  # noqa: E402


def _plain_tool(query: str) -> str:
    """Trivial tool with no dependencies."""
    return query


def test_log_agent_sets_experiment_when_provided() -> None:
    agent = Agent(tools=[_plain_tool])

    fake_log_model = MagicMock(return_value=MagicMock(registered_model_version="1"))

    with patch("mlflow.set_experiment") as mock_set, \
         patch("mlflow.pyfunc.log_model", fake_log_model), \
         patch("apx_agent._chat_agent.compile_to_chat_agent", return_value=MagicMock()):
        log_agent(
            agent,
            model="databricks-claude-sonnet-4-6",
            registered_model_name="main.agents.x",
            experiment="/Users/me/agents/x",
        )

    mock_set.assert_called_once_with("/Users/me/agents/x")


def test_log_agent_omits_set_experiment_when_not_provided() -> None:
    agent = Agent(tools=[_plain_tool])
    fake_log_model = MagicMock(return_value=MagicMock(registered_model_version="1"))

    with patch("mlflow.set_experiment") as mock_set, \
         patch("mlflow.pyfunc.log_model", fake_log_model), \
         patch("apx_agent._chat_agent.compile_to_chat_agent", return_value=MagicMock()):
        log_agent(
            agent,
            model="databricks-claude-sonnet-4-6",
            registered_model_name="main.agents.x",
        )

    mock_set.assert_not_called()


def test_log_agent_friendly_error_on_set_experiment_failure() -> None:
    agent = Agent(tools=[_plain_tool])

    with patch("mlflow.set_experiment", side_effect=RuntimeError("forbidden")):
        with pytest.raises(RuntimeError, match="mlflow.set_experiment"):
            log_agent(
                agent,
                model="databricks-claude-sonnet-4-6",
                registered_model_name="main.agents.x",
                experiment="/bad/path",
            )
