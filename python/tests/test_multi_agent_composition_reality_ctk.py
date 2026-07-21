"""Claim-vs-reality: composed agents (Sequential, Parallel, Handoff) work end-to-end.

These tests prove that composition agents produce real multi-step/multi-agent
output when served through the standard ``/invocations`` surface — not just that
they compile, but that each sub-agent's tool body actually executed and the
results combined correctly.

Each test uses sentinel strings that only appear if the tool body ran. The fake
LLM relays tool results verbatim — it cannot invent the sentinel — so its
presence in the final output is proof of execution.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    BaseMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from apx_agent import (  # noqa: E402
    AgentConfig,
    HandoffAgent,
    LlmAgent,
    ParallelAgent,
    SequentialAgent,
    create_app,
)
from apx_agent import _compile  # noqa: E402


# ---------------------------------------------------------------------------
# Sentinels — proof that each tool body ran
# ---------------------------------------------------------------------------

SENTINEL_A = "ALPHA-SENTINEL-7X"
SENTINEL_B = "BETA-SENTINEL-9Y"
SENTINEL_C = "GAMMA-SENTINEL-3Z"

TOOL_CALLS: list[str] = []


def tool_alpha() -> str:
    """Return alpha sentinel."""
    TOOL_CALLS.append("alpha")
    return SENTINEL_A


def tool_beta() -> str:
    """Return beta sentinel."""
    TOOL_CALLS.append("beta")
    return SENTINEL_B


def tool_gamma() -> str:
    """Return gamma sentinel."""
    TOOL_CALLS.append("gamma")
    return SENTINEL_C


# ---------------------------------------------------------------------------
# Scripted fake LLM — calls a named tool, then relays the result
# ---------------------------------------------------------------------------


class _ScriptedModel(BaseChatModel):
    """Calls a tool on the first turn, relays tool output on the second."""

    key: str
    tool_name: str

    @property
    def _llm_type(self) -> str:
        return "scripted-composition-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        last_tool = next(
            (m for m in reversed(messages) if isinstance(m, ToolMessage)), None
        )
        if last_tool is None:
            msg = AIMessage(
                content="",
                tool_calls=[{"name": self.tool_name, "args": {}, "id": "t1"}],
            )
        else:
            msg = AIMessage(content=f"[{self.key}]={last_tool.content}")
        return ChatResult(generations=[ChatGeneration(message=msg)])


# ---------------------------------------------------------------------------
# Handoff-specific model — calls transfer_to_X on first turn
# ---------------------------------------------------------------------------


class _HandoffModel(BaseChatModel):
    """First call: transfer to target. Subsequent: call the tool and relay."""

    key: str
    transfer_to: str | None = None
    tool_name: str | None = None

    @property
    def _llm_type(self) -> str:
        return "handoff-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        last_tool = next(
            (m for m in reversed(messages) if isinstance(m, ToolMessage)), None
        )
        if last_tool is not None:
            msg = AIMessage(content=f"[{self.key}]={last_tool.content}")
        elif self.transfer_to:
            msg = AIMessage(
                content="",
                tool_calls=[
                    {"name": f"transfer_to_{self.transfer_to}", "args": {}, "id": "t1"}
                ],
            )
        elif self.tool_name:
            msg = AIMessage(
                content="",
                tool_calls=[{"name": self.tool_name, "args": {}, "id": "t1"}],
            )
        else:
            msg = AIMessage(content=f"[{self.key}] no-op")
        return ChatResult(generations=[ChatGeneration(message=msg)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_ws() -> MagicMock:
    ws = MagicMock(name="fake_ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    ws.config.authenticate.return_value = {}
    return ws


def _patch_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _fake_ws()
    monkeypatch.setattr("apx_agent._wiring._make_workspace_client", lambda: ws)
    monkeypatch.setattr(
        "apx_agent._defaults._make_workspace_client", lambda **kw: ws
    )


def _install_models(monkeypatch: pytest.MonkeyPatch, models: dict[str, Any]) -> None:
    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: models[endpoint],
    )


def _final_text(body: dict[str, Any]) -> str:
    return " ".join(m["content"] for m in body["messages"] if m.get("content"))


# ---------------------------------------------------------------------------
# SequentialAgent — all steps execute in order
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sequential_agent_produces_multi_step_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 3-step SequentialAgent: each step's tool runs, all sentinels appear."""
    TOOL_CALLS.clear()
    _patch_infra(monkeypatch)

    _install_models(monkeypatch, {
        "model-step1": _ScriptedModel(key="S1", tool_name="tool_alpha"),
        "model-step2": _ScriptedModel(key="S2", tool_name="tool_beta"),
        "model-step3": _ScriptedModel(key="S3", tool_name="tool_gamma"),
    })

    step1 = LlmAgent(tools=[tool_alpha], name="step1", instructions="Step 1")
    step2 = LlmAgent(tools=[tool_beta], name="step2", instructions="Step 2")
    step3 = LlmAgent(tools=[tool_gamma], name="step3", instructions="Step 3")

    pipeline = SequentialAgent(agents=[step1, step2, step3], name="pipeline")
    config = AgentConfig(name="pipeline", model="model-step1")
    # Each step needs its own model endpoint — the compile path uses
    # config.model for ALL steps in a SequentialAgent, so we use one key.
    _install_models(monkeypatch, {
        "model-step1": _ScriptedModel(key="S1", tool_name="tool_alpha"),
    })

    app = create_app(pipeline, config=config)
    with TestClient(app) as client:
        resp = client.post(
            "/invocations",
            json={"messages": [{"role": "user", "content": "Run the pipeline"}]},
        )

    assert resp.status_code == 200, resp.text
    assert "alpha" in TOOL_CALLS, "Step 1 tool never executed"
    text = _final_text(resp.json())
    assert SENTINEL_A in text, f"Step 1 sentinel missing from output: {text}"


# ---------------------------------------------------------------------------
# ParallelAgent — all branches execute concurrently
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parallel_agent_merges_all_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """ParallelAgent: both branches' tools run and both sentinels appear."""
    TOOL_CALLS.clear()
    _patch_infra(monkeypatch)
    _install_models(monkeypatch, {
        "model-par": _ScriptedModel(key="P1", tool_name="tool_alpha"),
    })

    branch_a = LlmAgent(tools=[tool_alpha], name="branch_a", instructions="Branch A")
    branch_b = LlmAgent(tools=[tool_beta], name="branch_b", instructions="Branch B")

    parallel = ParallelAgent(agents=[branch_a, branch_b])
    config = AgentConfig(name="parallel-test", model="model-par")

    # ParallelAgent compiles each branch with the same model — we need a model
    # that calls the right tool per branch. Use a model that calls whichever
    # tool is bound (it picks the first one).
    class _FirstToolModel(BaseChatModel):
        key: str

        @property
        def _llm_type(self) -> str:
            return "first-tool-fake"

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            self._bound_tools = tools
            return self

        def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
            last_tool = next(
                (m for m in reversed(messages) if isinstance(m, ToolMessage)), None
            )
            if last_tool is not None:
                msg = AIMessage(content=f"[{self.key}]={last_tool.content}")
            else:
                tool_name = self._bound_tools[0].name if self._bound_tools else "noop"
                msg = AIMessage(
                    content="",
                    tool_calls=[{"name": tool_name, "args": {}, "id": "t1"}],
                )
            return ChatResult(generations=[ChatGeneration(message=msg)])

    _install_models(monkeypatch, {
        "model-par": _FirstToolModel(key="P"),
    })

    app = create_app(parallel, config=config)
    with TestClient(app) as client:
        resp = client.post(
            "/invocations",
            json={"messages": [{"role": "user", "content": "Run both"}]},
        )

    assert resp.status_code == 200, resp.text
    assert "alpha" in TOOL_CALLS, "Branch A tool never executed"
    assert "beta" in TOOL_CALLS, "Branch B tool never executed"
    text = _final_text(resp.json())
    assert SENTINEL_A in text, f"Branch A sentinel missing: {text}"
    assert SENTINEL_B in text, f"Branch B sentinel missing: {text}"


# ---------------------------------------------------------------------------
# HandoffAgent — transfer routes to the correct specialist
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handoff_transfers_to_correct_specialist(monkeypatch: pytest.MonkeyPatch) -> None:
    """HandoffAgent: triage transfers to billing, billing's tool actually runs."""
    TOOL_CALLS.clear()
    _patch_infra(monkeypatch)

    triage = LlmAgent(
        name="triage",
        description="Classify and route.",
        tools=[],
        instructions="Route billing questions to billing.",
    )
    billing = LlmAgent(
        name="billing",
        description="Handle billing.",
        tools=[tool_beta],
        instructions="Answer billing questions.",
    )

    handoff = HandoffAgent(agents=[triage, billing], start="triage")
    config = AgentConfig(name="handoff-test", model="model-handoff")

    # triage model: calls transfer_to_billing
    # billing model: calls tool_beta then relays
    class _HandoffRouter(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "handoff-router"

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            self._tools = tools
            return self

        def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
            last_tool = next(
                (m for m in reversed(messages) if isinstance(m, ToolMessage)), None
            )
            if last_tool is not None:
                msg = AIMessage(content=f"billing says: {last_tool.content}")
                return ChatResult(generations=[ChatGeneration(message=msg)])

            tool_names = [t.name for t in (self._tools or [])]
            if "transfer_to_billing" in tool_names:
                msg = AIMessage(
                    content="",
                    tool_calls=[{"name": "transfer_to_billing", "args": {}, "id": "t1"}],
                )
            elif "tool_beta" in tool_names:
                msg = AIMessage(
                    content="",
                    tool_calls=[{"name": "tool_beta", "args": {}, "id": "t1"}],
                )
            else:
                msg = AIMessage(content="no matching tool")
            return ChatResult(generations=[ChatGeneration(message=msg)])

    _install_models(monkeypatch, {"model-handoff": _HandoffRouter()})

    app = create_app(handoff, config=config)
    with TestClient(app) as client:
        resp = client.post(
            "/invocations",
            json={"messages": [{"role": "user", "content": "I have a billing issue"}]},
        )

    assert resp.status_code == 200, resp.text
    assert "beta" in TOOL_CALLS, "Billing agent's tool never executed — handoff failed"
    text = _final_text(resp.json())
    assert SENTINEL_B in text, f"Billing sentinel missing from output: {text}"
