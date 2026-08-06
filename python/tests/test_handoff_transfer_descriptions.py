"""#634: compiled handoff transfer tools must carry specialist descriptions.

``HandoffAgent._transfer_tools_for`` (the in-process path) describes each
``transfer_to_<name>`` tool with that specialist's ``description=``, falling
back to generic text. ``_compile_handoff_agent`` hard-coded the generic text,
so the served/compiled path gave the routing LLM strictly less information than
the in-process path — same declaration, worse routing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from apx_agent import HandoffAgent, LlmAgent  # noqa: E402

BILLING_DESC = "Handles invoices, refunds, and payment disputes."
TECH_DESC = "Debugs product errors and integration failures."


def _noop_tool(query: str) -> str:
    """Echo the query."""
    return query


@pytest.fixture
def captured_tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture every ``transfer_to_*`` tool handed to create_agent by name."""
    import langchain.agents as _la

    from apx_agent import _compile

    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: MagicMock(
            name=f"fake_llm:{endpoint}"
        ),
    )

    transfers: dict[str, Any] = {}

    def fake_create_agent(**kwargs: Any) -> Any:
        for tool in kwargs.get("tools", []):
            name = getattr(tool, "name", "")
            if name.startswith(HandoffAgent.TRANSFER_PREFIX):
                transfers[name] = tool
        return MagicMock()

    monkeypatch.setattr(_la, "create_agent", fake_create_agent)
    return transfers


def _compile_handoff(specialist_descriptions: bool) -> HandoffAgent:
    from apx_agent._compile import CompileContext, _compile_handoff_agent

    agents = {
        "triage": LlmAgent(tools=[_noop_tool], instructions="Triage."),
        "billing": LlmAgent(
            tools=[_noop_tool],
            instructions="Billing.",
            description=BILLING_DESC if specialist_descriptions else None,
        ),
        "tech": LlmAgent(
            tools=[_noop_tool],
            instructions="Tech.",
            description=TECH_DESC if specialist_descriptions else None,
        ),
    }
    handoff = HandoffAgent(agents=agents, start="triage")
    _compile_handoff_agent(handoff, CompileContext(ws=MagicMock(), model="any"))
    return handoff


def test_compiled_transfer_tools_use_specialist_descriptions(
    captured_tools: dict[str, Any],
) -> None:
    """#634: the compiled tool description is the specialist's own description."""
    _compile_handoff(specialist_descriptions=True)

    assert captured_tools["transfer_to_billing"].description == BILLING_DESC
    assert captured_tools["transfer_to_tech"].description == TECH_DESC


def test_compiled_transfer_descriptions_match_in_process_path(
    captured_tools: dict[str, Any],
) -> None:
    """Compiled and in-process handoff paths must describe transfers identically."""
    handoff = _compile_handoff(specialist_descriptions=True)

    in_process = {
        tool.name: tool.description for tool in handoff._transfer_tools_for("triage")
    }
    for name, expected in in_process.items():
        assert captured_tools[name].description == expected, (
            f"{name}: compiled description {captured_tools[name].description!r} "
            f"diverges from in-process {expected!r} (#634)"
        )


def test_compiled_transfer_tools_fall_back_when_no_description(
    captured_tools: dict[str, Any],
) -> None:
    """Without a specialist description, keep the generic handoff text."""
    _compile_handoff(specialist_descriptions=False)

    assert captured_tools["transfer_to_billing"].description == (
        "Hand off to the billing agent."
    )
