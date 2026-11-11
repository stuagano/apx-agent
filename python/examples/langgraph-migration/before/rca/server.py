"""The original self-hosted HTTP surface, replaced by DurableAgentServer in after/."""

from fastapi import FastAPI
from pydantic import BaseModel

from rca.graph import build_graph

app = FastAPI()
graph = build_graph()


class RcaRequest(BaseModel):
    tenant: str
    thread_id: str
    question: str = "Why are KPIs off?"


@app.post("/rca")
def rca(req: RcaRequest) -> dict:
    out = graph.invoke(
        {"messages": [("user", req.question)], "tenant": req.tenant},
        {"configurable": {"thread_id": req.thread_id}},
    )
    return {"anomalies": out["anomalies"], "report": out["messages"][-1].content}
