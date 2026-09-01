"""Tests for _executor_factory — governance-safe executor selection (#463).

The ``claude-sdk`` executor calls tool functions directly and runs none of the
``before_tool`` / ``before_model`` / guardrail wiring that PolicyGate (approval),
WatchdogGuard, tool allow/deny lists, rate limits, and the injection heuristic
compose into. Selecting it must therefore NOT silently disable configured
governance — the factory falls back to the governed LangGraph path instead.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from apx_agent import LlmAgent  # noqa: E402
from apx_agent._models import AgentConfig, GuardrailsConfig  # noqa: E402
from apx_agent._executor_factory import (  # noqa: E402
    create_executor,
    governance_guards_present,
)
from apx_agent._langgraph_executor import LangGraphExecutor  # noqa: E402


def _noop(**_kw: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# governance_guards_present — detection
# ---------------------------------------------------------------------------


def test_clean_agent_has_no_governance() -> None:
    assert governance_guards_present(LlmAgent(tools=[]), AgentConfig(name="a", model="m")) is None


@pytest.mark.parametrize(
    "kwarg",
    ["before_tool", "before_model", "input_guardrails", "output_guardrails"],
)
def test_agent_hook_is_detected(kwarg: str) -> None:
    value = [_noop] if kwarg.endswith("guardrails") else _noop
    agent = LlmAgent(tools=[], **{kwarg: value})
    assert governance_guards_present(agent) is not None


@pytest.mark.parametrize(
    "guardrails",
    [
        GuardrailsConfig(blocked_tools=["delete"]),
        GuardrailsConfig(allowed_tools=["safe"]),
        GuardrailsConfig(rate_limit=60),
        GuardrailsConfig(injection_detection=True),
    ],
)
def test_config_guardrails_are_detected(guardrails: GuardrailsConfig) -> None:
    agent = LlmAgent(tools=[])
    config = AgentConfig(name="a", model="m", guardrails=guardrails)
    assert governance_guards_present(agent, config) is not None


# ---------------------------------------------------------------------------
# create_executor — the security-relevant fallback
# ---------------------------------------------------------------------------


def test_langgraph_executor_receives_distinct_clients() -> None:
    agent = LlmAgent(tools=[])
    config = AgentConfig(name="a", model="m")
    user_ws = object()
    service_ws = object()

    executor = create_executor(agent, config, ws=user_ws, service_ws=service_ws)

    assert isinstance(executor, LangGraphExecutor)
    assert executor._user_ws is user_ws
    assert executor._service_ws is service_ws


def test_claude_sdk_with_governance_falls_back_to_langgraph() -> None:
    """A guarded agent asking for claude-sdk must run the GOVERNED path."""
    agent = LlmAgent(tools=[], before_tool=_noop)
    config = AgentConfig(name="a", model="m", executor="claude-sdk")
    assert isinstance(create_executor(agent, config), LangGraphExecutor)


def test_claude_sdk_with_config_guardrails_falls_back_to_langgraph() -> None:
    agent = LlmAgent(tools=[])
    config = AgentConfig(
        name="a", model="m", executor="claude-sdk",
        guardrails=GuardrailsConfig(blocked_tools=["delete"]),
    )
    assert isinstance(create_executor(agent, config), LangGraphExecutor)


def test_clean_claude_sdk_agent_gets_sdk_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """No governance → the SDK executor is honoured (patched to avoid the SDK dep)."""
    sentinel = object()
    monkeypatch.setattr(
        "apx_agent._claude_sdk_executor.ClaudeSDKExecutor",
        lambda **_kw: sentinel,
    )
    agent = LlmAgent(tools=[])
    config = AgentConfig(name="a", model="m", executor="claude-sdk")
    assert create_executor(agent, config) is sentinel
