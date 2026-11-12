"""GraphAgent (#1037, part C): crash recovery resumes a hand-written graph from its checkpoint.

Uses the real SDK ``DatabricksSessionStoreSaver`` over persisted REST records, and rebuilds the
handlers and saver between attempts, so no process-local checkpoint cache survives.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, TypedDict

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.graph.message import add_messages  # noqa: E402

from apx_agent import GraphAgent, RuntimeRequirements, inspect_target  # noqa: E402
from apx_agent._durable_agent import compile_durable_handlers  # noqa: E402
from tests.test_durable_recovery import PAYLOAD, checkpoints, context  # noqa: E402,F401
from tests.test_runtime_targets import execution  # noqa: E402,F401


class RcaState(TypedDict):
    messages: Annotated[list, add_messages]
    kpis: dict
    anomalies: list


def _rca_agent(runs: dict[str, int], *, fail_analyze_once: bool) -> GraphAgent:
    failed = {"analyze": False}

    def build(tools: list[Any], checkpointer: Any) -> Any:
        def compute(_state: RcaState) -> dict[str, Any]:
            runs["compute"] += 1
            return {"kpis": {"forecast_accuracy": 0.62, "fill_rate": 0.97}}

        def detect(state: RcaState) -> dict[str, Any]:
            runs["detect"] += 1
            return {"anomalies": sorted(k for k, v in state["kpis"].items() if v < 0.8)}

        def analyze(state: RcaState) -> dict[str, Any]:
            runs["analyze"] += 1
            if fail_analyze_once and not failed["analyze"]:
                failed["analyze"] = True
                raise ConnectionError("worker lost before analyze finished")
            return {"messages": [AIMessage(content=f"root cause for {state['anomalies']}")]}

        graph = StateGraph(RcaState)
        for name, node in (("compute", compute), ("detect", detect), ("analyze", analyze)):
            graph.add_node(name, node)
        graph.add_edge(START, "compute")
        graph.add_edge("compute", "detect")
        graph.add_edge("detect", "analyze")
        graph.add_edge("analyze", END)
        return graph.compile(checkpointer=checkpointer)

    return GraphAgent(build=build, name="proof", output=lambda state: {"anomalies": state["anomalies"]})


def _handlers(agent: GraphAgent, execution: Any, checkpoints: Any) -> Any:
    return compile_durable_handlers(agent, model="test", service_ws=execution.ws,
                                    checkpointer=checkpoints(), recovery=True)


def test_recovery_resumes_the_users_graph_without_rerunning_finished_nodes(
    execution: Any, checkpoints: Any,
) -> None:
    runs = {"compute": 0, "detect": 0, "analyze": 0}
    agent = _rca_agent(runs, fail_analyze_once=True)

    with pytest.raises(ConnectionError, match="worker lost"):
        asyncio.run(_handlers(agent, execution, checkpoints).invoke(PAYLOAD, context()))
    assert runs == {"compute": 1, "detect": 1, "analyze": 1}

    # Fresh handlers and saver: only the persisted checkpoint carries progress.
    result = asyncio.run(_handlers(agent, execution, checkpoints).invoke(PAYLOAD, context(recovery=True)))

    assert result["status"] == "completed"
    assert result["output"] == {"anomalies": ["forecast_accuracy"]}
    assert result["messages"][-1]["content"] == "root cause for ['forecast_accuracy']"
    assert runs == {"compute": 1, "detect": 1, "analyze": 2}  # resumed at analyze

    again = asyncio.run(_handlers(agent, execution, checkpoints).invoke(PAYLOAD, context(recovery=True)))
    assert again == result
    assert runs["analyze"] == 2


def test_recovery_refuses_a_changed_input(execution: Any, checkpoints: Any) -> None:
    runs = {"compute": 0, "detect": 0, "analyze": 0}
    agent = _rca_agent(runs, fail_analyze_once=True)
    with pytest.raises(ConnectionError):
        asyncio.run(_handlers(agent, execution, checkpoints).invoke(PAYLOAD, context()))

    with pytest.raises(ValueError, match="input differs"):
        asyncio.run(_handlers(agent, execution, checkpoints).invoke(
            [{"role": "user", "content": "changed"}], context(recovery=True)))


def test_graph_agent_supports_recovery_on_the_durable_target(checkpoints: Any) -> None:
    runs = {"compute": 0, "detect": 0, "analyze": 0}
    report = inspect_target(_rca_agent(runs, fail_analyze_once=False), target="durable_agent_server",
                            checkpointer=checkpoints(), requirements=RuntimeRequirements(recovery=True))

    report.require_compatible()


def test_a_policy_ask_interrupts_the_users_graph_and_resume_runs_the_tool(
    execution: Any, checkpoints: Any,
) -> None:
    from langgraph.graph import MessagesState
    from langgraph.prebuilt import ToolNode

    from apx_agent import FunctionPolicy, PolicyAction, PolicyGate, PolicyResult

    effects: list[str] = []

    def record(value: str) -> str:
        """Record a value."""
        effects.append(value)
        return f"recorded {value}"

    def build(tools: list[Any], checkpointer: Any) -> Any:
        def plan(_state: MessagesState) -> dict[str, Any]:
            call = {"name": "record", "args": {"value": "proof"}, "id": "call-1"}
            return {"messages": [AIMessage(content="", tool_calls=[call])]}

        graph = StateGraph(MessagesState)
        graph.add_node("plan", plan)
        graph.add_node("tools", ToolNode(tools))
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "tools")
        graph.add_edge("tools", END)
        return graph.compile(checkpointer=checkpointer)

    gate = PolicyGate([FunctionPolicy(lambda event: PolicyResult(action=PolicyAction.ASK, reason="approval required"))])
    agent = GraphAgent(build=build, tools=[record], name="proof", before_tool=gate)

    first = asyncio.run(_handlers(agent, execution, checkpoints).invoke(PAYLOAD, context()))
    assert first["status"] == "interrupted"
    assert first["approval_required"]["tool_name"] == "record"
    assert effects == []

    resumed = asyncio.run(_handlers(agent, execution, checkpoints).invoke({"resume": "approve"},
                                                                          context("approval-two")))
    assert resumed["status"] == "completed"
    assert effects == ["proof"]
    assert resumed["messages"][-1]["content"] == "recorded proof"


def test_a_checkpointed_graph_agent_reports_approvals_as_supported(checkpoints: Any) -> None:
    runs = {"compute": 0, "detect": 0, "analyze": 0}
    report = inspect_target(_rca_agent(runs, fail_analyze_once=False), target="durable_agent_server",
                            checkpointer=checkpoints())

    assert report.capabilities["approvals"].supported


# --- Session budgets, ToolScope ceilings and principal-keyed threads (#1037 follow-up) -----------


class BudgetState(TypedDict):
    messages: Annotated[list, add_messages]
    state: dict  # where apx persists the session's running token total


def _spending_agent(runs: list[int], *, tokens_per_turn: int, budget: int, state_channel: bool = True) -> GraphAgent:
    from langgraph.graph import MessagesState

    def build(tools: list[Any], checkpointer: Any) -> Any:
        def spend(_state: Any) -> dict[str, Any]:
            runs.append(1)
            usage = {"input_tokens": tokens_per_turn, "output_tokens": 0, "total_tokens": tokens_per_turn}
            return {"messages": [AIMessage(content="ok", usage_metadata=usage)]}

        graph = StateGraph(BudgetState if state_channel else MessagesState)
        graph.add_node("spend", spend)
        graph.add_edge(START, "spend")
        graph.add_edge("spend", END)
        return graph.compile(checkpointer=checkpointer)

    return GraphAgent(build=build, name="proof", session_budget={"tokens": budget})


def test_a_session_budget_caps_a_graph_agents_cumulative_tokens(execution: Any, checkpoints: Any) -> None:
    from apx_agent._errors import SessionBudgetExceeded

    runs: list[int] = []
    agent = _spending_agent(runs, tokens_per_turn=6, budget=10)

    first = asyncio.run(_handlers(agent, execution, checkpoints).invoke(PAYLOAD, context("turn-1")))
    assert first["status"] == "completed"  # 6 of 10
    with pytest.raises(SessionBudgetExceeded):  # 12 crosses 10 after the turn
        asyncio.run(_handlers(agent, execution, checkpoints).invoke(PAYLOAD, context("turn-2")))
    with pytest.raises(SessionBudgetExceeded):  # already over: refused before running
        asyncio.run(_handlers(agent, execution, checkpoints).invoke(PAYLOAD, context("turn-3")))

    assert len(runs) == 2


def test_a_session_budget_needs_a_state_channel_to_persist_its_total(execution: Any) -> None:
    from apx_agent import compile_to_langgraph

    agent = _spending_agent([], tokens_per_turn=1, budget=10, state_channel=False)

    with pytest.raises(ValueError, match="session_budget needs a `state: dict` channel"):
        compile_to_langgraph(agent, ws=execution.ws, model="m")


def test_an_invalid_session_budget_is_rejected_like_an_llm_agents() -> None:
    with pytest.raises(ValueError, match=r"session_budget\['tokens'\] must be a positive int"):
        GraphAgent(build=lambda tools, checkpointer: None, session_budget={"tokens": 0})


def test_a_toolscope_ceiling_blocks_an_out_of_scope_call_inside_the_users_graph(execution: Any) -> None:
    from langgraph.graph import MessagesState
    from langgraph.prebuilt import ToolNode

    from apx_agent import compile_to_langgraph
    from apx_agent._tool_scope import ToolScope, attach_scope, attach_scope_guard

    reads: list[str] = []

    def ledger_reader(table_name: str) -> str:
        """Read a UC table."""
        reads.append(table_name)
        return "rows"

    attach_scope(ledger_reader, ToolScope(catalogs=("sales",)))

    def build(tools: list[Any], checkpointer: Any) -> Any:
        def plan(_state: MessagesState) -> dict[str, Any]:
            call = {"name": "ledger_reader", "args": {"table_name": "main.finance.ledger"}, "id": "c1"}
            return {"messages": [AIMessage(content="", tool_calls=[call])]}

        graph = StateGraph(MessagesState)
        graph.add_node("plan", plan)
        graph.add_node("tools", ToolNode(tools))
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "tools")
        graph.add_edge("tools", END)
        return graph.compile(checkpointer=checkpointer)

    agent = GraphAgent(build=build, tools=[ledger_reader], name="proof")
    assert attach_scope_guard(agent)  # the same wiring apx applies at finalize

    out = compile_to_langgraph(agent, ws=execution.ws, model="m").invoke({"messages": [("user", "go")]})

    assert reads == []
    assert out["messages"][-1].status == "error"
    assert "main.finance.ledger" in out["messages"][-1].content


def test_two_callers_on_the_same_session_id_get_separate_threads(
    execution: Any, checkpoints: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from databricks_agentkit.runtime.auth import RequestAuthContext
    from langgraph.graph import MessagesState

    seen: list[int] = []

    def build(tools: list[Any], checkpointer: Any) -> Any:
        def count(state: MessagesState) -> dict[str, Any]:
            seen.append(len(state["messages"]))
            return {"messages": [AIMessage(content="ok")]}

        graph = StateGraph(MessagesState)
        graph.add_node("count", count)
        graph.add_edge(START, "count")
        graph.add_edge("count", END)
        return graph.compile(checkpointer=checkpointer)

    def user_client(auth: Any, mode: str) -> Any:
        user = MagicMock()
        user.current_user.me.return_value.id = auth.namespace("principal", "verified")
        return user

    monkeypatch.setattr(RequestAuthContext, "client_for", user_client)
    monkeypatch.setenv("DATABRICKS_APP_NAME", "proof")
    agent = GraphAgent(build=build, name="proof")
    handlers = compile_durable_handlers(agent, model="test", service_ws=execution.ws,
                                        checkpointer=checkpoints(), require_user=True)

    def as_user(principal: str, invocation: str) -> Any:
        auth = RequestAuthContext(token="sentinel", principal=principal, local=False)
        return SimpleNamespace(invocation_id=invocation, session_id="shared-session", request_auth=auth,
                               is_recovery=False)

    for principal, invocation in (("alice", "a-1"), ("bob", "b-1"), ("alice", "a-2")):
        asyncio.run(handlers.invoke(PAYLOAD, as_user(principal, invocation)))

    assert seen == [1, 1, 3]  # bob starts fresh; alice's second turn sees only her own history
