"""GraphAgent (#1037, part B): serve a hand-written graph on the durable target.

Typed input lands on the graph's own state keys (``input=``) and ``output=`` maps its
final state to the response, so the author's graph stays unchanged.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Any, TypedDict
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langgraph")

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.graph.message import add_messages  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402
from pydantic import BaseModel, ConfigDict  # noqa: E402

from apx_agent import GraphAgent, compile_agent  # noqa: E402
from apx_agent._native_input import native_data_part  # noqa: E402

THRESHOLD = 0.8
KPIS = {"acme": {"forecast_accuracy": 0.62, "fill_rate": 0.97}}


class RcaState(TypedDict):
    messages: Annotated[list, add_messages]
    tenant: str
    anomalies: list


class RcaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str


def _rca_build(tools: list[Any], checkpointer: Any) -> Any:
    """A hand-written graph that reads its own `tenant` key and writes `anomalies`."""

    def detect(state: RcaState) -> dict[str, Any]:
        anomalies = sorted(k for k, v in KPIS[state["tenant"]].items() if v < THRESHOLD)
        return {"anomalies": anomalies, "messages": [AIMessage(content=f"{len(anomalies)} anomalies")]}

    graph = StateGraph(RcaState)
    graph.add_node("detect", detect)
    graph.add_edge(START, "detect")
    graph.add_edge("detect", END)
    return graph.compile(checkpointer=checkpointer)


def _tool_build(tools: list[Any], checkpointer: Any) -> Any:
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


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Compile a GraphAgent for the durable target and yield a test client."""
    (tmp_path / "pyproject.toml").write_text('[tool.apx.deploy]\nname = "proof"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")
    ws = MagicMock()
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **k: ws)
    clients: list[Any] = []

    def _serve(agent: GraphAgent) -> TestClient:
        app = compile_agent(agent, target="durable_agent_server", model="test", service_ws=ws,
                            checkpointer=InMemorySaver())
        client = TestClient(app)
        clients.append(client.__enter__())
        return client

    yield _serve
    for client in clients:
        client.__exit__(None, None, None)


def _invoke(client: TestClient, session: str, value: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/api/invocations", json={"id": str(uuid.uuid4()), "session_id": session, "input": value})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed", body
    return body["output"]


def test_graph_agent_serves_and_runs_its_governed_tool(serve: Any) -> None:
    effects: list[str] = []

    def record(value: str) -> str:
        """Record a value."""
        effects.append(value)
        return f"recorded {value}"

    client = serve(GraphAgent(build=_tool_build, tools=[record], name="proof"))
    output = _invoke(client, "s-1", {"messages": [{"role": "user", "content": "go"}]})

    assert effects == ["proof"]
    assert output["messages"][-1]["content"] == "recorded proof"


def test_typed_input_lands_on_the_graphs_own_state_and_output_shapes_the_response(serve: Any) -> None:
    agent = GraphAgent(build=_rca_build, name="rca", input_schema=RcaInput,
                       input=lambda data: {"tenant": data["tenant"]},
                       output=lambda state: {"tenant": state["tenant"], "anomalies": state["anomalies"]})
    client = serve(agent)
    part = native_data_part(RcaInput, {"tenant": "acme"})

    output = _invoke(client, "s-rca", {"messages": [{"role": "user", "content": "why?"}], "apx_data_parts": [part]})

    assert output["output"] == {"tenant": "acme", "anomalies": ["forecast_accuracy"]}
    assert output["messages"][-1]["content"] == "1 anomalies"


def test_a_second_turn_resumes_the_sessions_checkpoint(serve: Any) -> None:
    turns: list[int] = []

    def build(tools: list[Any], checkpointer: Any) -> Any:
        def count(state: MessagesState) -> dict[str, Any]:
            turns.append(len(state["messages"]))
            return {"messages": [AIMessage(content="ok")]}

        graph = StateGraph(MessagesState)
        graph.add_node("count", count)
        graph.add_edge(START, "count")
        graph.add_edge("count", END)
        return graph.compile(checkpointer=checkpointer)

    client = serve(GraphAgent(build=build, name="memory"))
    for _ in range(2):
        _invoke(client, "s-same", {"messages": [{"role": "user", "content": "hi"}]})

    assert turns == [1, 3]  # turn 2 sees turn 1's user + assistant messages


def test_input_requires_an_input_schema() -> None:
    with pytest.raises(ValueError, match="input= requires input_schema"):
        GraphAgent(build=_rca_build, input=lambda data: data)


def test_output_must_be_callable() -> None:
    with pytest.raises(TypeError, match="output= must be callable"):
        GraphAgent(build=_rca_build, output="anomalies")  # type: ignore[arg-type]


def test_a_deployed_managed_session_store_binds_to_a_graph_agent() -> None:
    from apx_agent._runtime_targets import inspect_target

    report = inspect_target(GraphAgent(build=_rca_build, name="rca"), target="durable_agent_server",
                            session_store="rca-sessions")

    report.require_compatible()

