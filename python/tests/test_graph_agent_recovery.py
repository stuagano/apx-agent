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
