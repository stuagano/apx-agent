"""AC-8: MLflow trace parentage across the A2A hop is intact for a control reply.

Gate for prd_a2a-control-result.md AC-8 — regenerate from the PRD, don't hand-edit.

This is the one AC the shared mock harness (``_a2a_control_helpers``, which patches
``RemoteDatabricksAgent.run_with_control``) cannot exercise: cross-app trace
parentage requires a *real* A2A hop between two ASGI apps.

A served agent whose turn ends on a ``finish_loop`` sentinel already surfaces it
as a ``function_call`` output item (``_langchain_to_output_item``), so the caller's
``_extract_remote_control`` reconstructs it off the real ``/responses`` reply — the
server-side emit (FR-2) rides the existing egress, no new code. This gate proves it
over a genuine two-ASGI-app hop AND that the outbound leg carries the caller's
``traceparent`` (W3C linkage), so the remote span joins the caller's trace (NFR-3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import mlflow
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from apx_agent import (
    Agent,
    AgentConfig,
    LoopAgent,
    compile_to_langgraph,
    create_app,
    finalize_agent,
)
from apx_agent._audit import TRACEPARENT_HEADER

CARD_URL = "http://loop-body.internal/.well-known/agent.json"


class _FinishLoopModel(BaseChatModel):
    """Served loop-body model: ends its turn on a ``finish_loop`` sentinel."""

    @property
    def _llm_type(self) -> str:
        return "ac8-finish-loop"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(
        self, messages: list[Any], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        # After the sentinel round-trips through the served tool node, answer.
        if messages and isinstance(messages[-1], ToolMessage):
            message: AIMessage = AIMessage(content="loop body done")
        else:
            message = AIMessage(
                content="",
                tool_calls=[{"name": LoopAgent.FINISH_TOOL, "args": {}, "id": "c1"}],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


class _RecordingASGITransport(httpx.AsyncBaseTransport):
    def __init__(self, app: Any) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._inner.handle_async_request(request)


def _route_async_clients(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport
) -> None:
    real_async_client = httpx.AsyncClient

    class _RoutedAsyncClient(real_async_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("transport", transport)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _RoutedAsyncClient)


def _last_tool_names(state: dict[str, Any]) -> list[str]:
    for message in reversed(state["messages"]):
        if isinstance(message, AIMessage) and message.tool_calls:
            return [tc["name"] for tc in message.tool_calls]
    return []


def test_cross_app_trace_parentage_control(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Served side: a real ASGI app whose model ends on a finish_loop sentinel.
    ws = MagicMock(name="offline_workspace")
    ws.config.host = "https://workspace.example"
    ws.config.authenticate.return_value = {}
    monkeypatch.setattr("apx_agent._wiring._make_workspace_client", lambda: ws)
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda *a, **kw: ws)
    monkeypatch.setattr(
        "apx_agent._compile._build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: _FinishLoopModel(),
    )

    served = create_app(
        Agent(name="body"), config=AgentConfig(name="loop-body", model="served-model")
    )

    # Caller side: a LoopAgent whose body is the remote served app, over the wire.
    monkeypatch.setenv("BODY_URL", CARD_URL)
    root = LoopAgent(Agent(name="body"), max_iterations=5)
    finalize_agent(
        root,
        AgentConfig(name="ctrl-ac8", bindings={"body": "$BODY_URL"}),
        pyproject_path=str(tmp_path / "missing.toml"),
    )
    graph = compile_to_langgraph(root, ws=None, model="unused-model")

    old_uri = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f"file://{tmp_path / 'mlruns'}")
    mlflow.set_experiment("a2a-control-result-ac8")
    try:
        with TestClient(served):
            transport = _RecordingASGITransport(served)
            _route_async_clients(monkeypatch, transport)
            with mlflow.start_span(name="caller-ingress") as sender:
                sender_trace = str(sender.trace_id).removeprefix("tr-").lower()
                state = graph.invoke({"messages": [HumanMessage(content="iterate")]})
    finally:
        mlflow.set_tracking_uri(old_uri)

    posts = [request for request in transport.requests if request.method == "POST"]

    # (1) The control signal routed over the REAL wire: one remote turn, then the
    # loop terminated on finish_loop — not after exhausting max_iterations.
    assert len(posts) == 1, f"expected one remote loop turn, saw {len(posts)}"
    assert LoopAgent.FINISH_TOOL in _last_tool_names(state)

    # (2) NFR-3: the outbound control hop carried the caller's W3C traceparent,
    # so the remote leg joins the caller's trace (same trace-id) — cross-app
    # parentage holds for a control reply, not only a sequential one.
    traceparent = posts[0].headers.get(TRACEPARENT_HEADER)
    assert traceparent is not None, "control hop sent no traceparent"
    segments = traceparent.split("-")
    assert len(segments) == 4, f"malformed traceparent {traceparent!r}"
    assert segments[1] == sender_trace, "remote control hop is not in the caller's trace"
