"""G1 interim warning — serve-time detection of guardrails / agent callbacks
that the served (/invocations, /responses) path does not yet enforce.

See docs/design/served-path-guards-and-identity.md.
"""

from __future__ import annotations

import logging

from apx_agent import LlmAgent, SequentialAgent, prompt_injection_heuristic
from apx_agent._agents import serve_unenforced_hooks, warn_unenforced_serve_hooks


def test_plain_agent_has_no_unenforced_hooks() -> None:
    agent = LlmAgent(tools=[], name="plain", instructions="Help.")
    assert serve_unenforced_hooks(agent) == []


def test_detects_input_guardrail_on_leaf_agent() -> None:
    agent = LlmAgent(
        tools=[],
        name="guarded",
        instructions="Help.",
        input_guardrails=[prompt_injection_heuristic()],
    )
    found = serve_unenforced_hooks(agent)
    assert found == [("guarded", ["input_guardrails"])]


def test_detects_before_after_callbacks() -> None:
    agent = LlmAgent(
        tools=[],
        name="hooked",
        instructions="Help.",
        before_agent_callback=lambda *a, **k: None,
        after_agent_callback=lambda *a, **k: None,
    )
    [(name, hooks)] = serve_unenforced_hooks(agent)
    assert name == "hooked"
    assert set(hooks) == {"before_agent_callback", "after_agent_callback"}


def test_walks_into_composite_sub_agents() -> None:
    """A guard on a sub-agent inside a composite must still be found."""
    inner = LlmAgent(
        tools=[],
        name="inner",
        instructions="Step.",
        input_guardrails=[prompt_injection_heuristic()],
    )
    plain = LlmAgent(tools=[], name="other", instructions="Step.")
    seq = SequentialAgent(agents=[plain, inner], instructions="Pipeline.")
    found = serve_unenforced_hooks(seq)
    assert ("inner", ["input_guardrails"]) in found
    # the plain sub-agent and the composite itself contribute nothing
    assert all(name == "inner" for name, _ in found)


def test_warn_logs_when_hooks_present(caplog) -> None:
    agent = LlmAgent(
        tools=[],
        name="guarded",
        instructions="Help.",
        input_guardrails=[prompt_injection_heuristic()],
    )
    with caplog.at_level(logging.WARNING, logger="apx_agent._agents"):
        warn_unenforced_serve_hooks(agent, surface="/invocations (ChatAgent)")
    assert any(
        "does NOT enforce them" in r.message and "guarded" in r.getMessage()
        for r in caplog.records
    )


def test_warn_silent_when_no_hooks(caplog) -> None:
    agent = LlmAgent(tools=[], name="plain", instructions="Help.")
    with caplog.at_level(logging.WARNING, logger="apx_agent._agents"):
        warn_unenforced_serve_hooks(agent, surface="/responses")
    assert caplog.records == []
