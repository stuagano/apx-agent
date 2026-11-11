"""Root-cause-analysis graph: compute KPIs, flag anomalies, ask an LLM why."""

from typing import Annotated, TypedDict

from databricks_langchain import ChatDatabricks
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

MODEL = "databricks-claude-sonnet-4-6"  # any chat model serving endpoint

# Fake per-tenant KPI feed; a real agent would query the tenant's catalog.
KPIS = {
    "acme": {"forecast_accuracy": 0.62, "fill_rate": 0.97, "inventory_turns": 8.1},
    "globex": {"forecast_accuracy": 0.91, "fill_rate": 0.81, "inventory_turns": 3.2},
}
THRESHOLDS = {"forecast_accuracy": 0.8, "fill_rate": 0.9, "inventory_turns": 5.0}
BEFORE_ANALYZE = lambda: None  # test hook; the runtime adapter sets it


class RcaState(TypedDict):
    messages: Annotated[list, add_messages]
    tenant: str
    kpis: dict
    anomalies: list


def compute_kpis(state: RcaState) -> dict:
    return {"kpis": KPIS[state["tenant"]]}


def detect(state: RcaState) -> dict:
    return {"anomalies": [k for k, v in state["kpis"].items() if v < THRESHOLDS[k]]}


def analyze(state: RcaState) -> dict:
    BEFORE_ANALYZE()
    prompt = (
        f"Tenant {state['tenant']} KPIs: {state['kpis']}. Thresholds: {THRESHOLDS}. "
        f"Anomalous: {state['anomalies']}. Give a 3-bullet root-cause hypothesis."
    )
    reply = ChatDatabricks(endpoint=MODEL).invoke(
        [SystemMessage("You are a supply-chain RCA analyst."), HumanMessage(prompt)]
    )
    return {"messages": [AIMessage(reply.content)]}


def build_graph(checkpointer=None):
    g = StateGraph(RcaState)
    g.add_node("compute_kpis", compute_kpis)
    g.add_node("detect", detect)
    g.add_node("analyze", analyze)
    g.add_edge(START, "compute_kpis")
    g.add_edge("compute_kpis", "detect")
    g.add_conditional_edges("detect", lambda s: "analyze" if s["anomalies"] else END)
    g.add_edge("analyze", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())
