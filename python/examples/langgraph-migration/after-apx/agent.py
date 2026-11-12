"""The unchanged RCA graph (rca/graph.py) as an apx GraphAgent."""

from pydantic import BaseModel, ConfigDict

from apx_agent import GraphAgent
from rca.graph import build_graph


class RcaInput(BaseModel):
    """What a caller sends, replacing the old POST /rca body."""

    model_config = ConfigDict(extra="forbid")
    tenant: str


agent = GraphAgent(
    build=lambda tools, checkpointer: build_graph(checkpointer),  # the graph binds no tools
    name="langgraph-rca",
    input_schema=RcaInput,
    input=lambda data: {"tenant": data["tenant"]},
    output=lambda state: {"anomalies": state["anomalies"], "report": state["messages"][-1].content},
)
