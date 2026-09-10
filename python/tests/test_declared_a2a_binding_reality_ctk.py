"""Ctk reality proof for a generated, declaratively bound internal A2A leaf."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import mlflow
import pytest
from ctk import claim_vs_reality, expect
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from apx_agent import (
    Agent,
    AgentConfig,
    DataAgent,
    Dependencies,
    RouterAgent,
    SequentialAgent,
    create_app,
    inject_tracing_headers,
)
from apx_agent._inspection import _load_agent_config
from apx_agent._project_gen import generate_project
from apx_agent._topology import build_topology


CARD_URL = "http://pricing.internal/.well-known/agent.json"
OBO_TOKEN = "opaque-user-token"
DIRECT_RESULT = "LOCAL_CAPABILITY_RESULT"
REMOTE_RESULT = "INTERNAL_PRICING_RESULT"

DIRECT_CALLS: list[str] = []
REVIEW_CALLS: list[str] = []
REMOTE_CALLS: list[str] = []
REMOTE_TOKENS: list[str | None] = []


@dataclass(frozen=True)
class _RequestResult:
    response: httpx.Response
    sender: Any


def read_capability(question: str) -> str:
    """Read a local data capability without delegating."""
    DIRECT_CALLS.append(question)
    return DIRECT_RESULT


def approved_price(headers: Dependencies.Headers) -> str:
    """Return a deterministic price while observing the caller identity."""
    REMOTE_CALLS.append("ran")
    REMOTE_TOKENS.append(
        headers.token.get_secret_value() if headers.token is not None else None
    )
    return REMOTE_RESULT


def _tool_name(tool: Any) -> str:
    if hasattr(tool, "name"):
        return str(tool.name)
    return str(tool["function"]["name"])


class _GraphModel(BaseChatModel):
    """Deterministic model whose choices are driven by the actually bound tools."""

    endpoint: str
    bound_tool_names: tuple[str, ...] = ()

    @property
    def _llm_type(self) -> str:
        return "declared-a2a-reality"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self.model_copy(
            update={"bound_tool_names": tuple(_tool_name(tool) for tool in tools)}
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        names = set(self.bound_tool_names)
        if any(name.startswith("transfer_to_") for name in names):
            query = next(
                (
                    str(message.content)
                    for message in reversed(messages)
                    if isinstance(message, HumanMessage)
                ),
                "",
            )
            route = "pricing_flow" if "price" in query.lower() else "data"
            message = AIMessage(
                content="",
                tool_calls=[
                    {"name": f"transfer_to_{route}", "args": {}, "id": "route-1"}
                ],
            )
        elif messages and isinstance(messages[-1], ToolMessage):
            message = AIMessage(content=str(messages[-1].content))
        elif not names:
            REVIEW_CALLS.append("observed request")
            message = AIMessage(content="reviewed")
        else:
            selected = next(
                name for name in ("read_capability", "approved_price") if name in names
            )
            args = (
                {"question": "observed request"} if selected != "approved_price" else {}
            )
            message = AIMessage(
                content="",
                tool_calls=[{"name": selected, "args": args, "id": f"{selected}-1"}],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


class _RecordingASGITransport(httpx.AsyncBaseTransport):
    def __init__(self, app: Any) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._inner.handle_async_request(request)


def _patch_workspace_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = MagicMock(name="offline_workspace")
    ws.config.host = "https://workspace.example"
    ws.config.authenticate.return_value = {}
    monkeypatch.setattr("apx_agent._wiring._make_workspace_client", lambda: ws)
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **kw: ws)
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda *a, **kw: ws)


def _patch_models(monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import _compile

    models = {
        "caller-model": _GraphModel(endpoint="caller"),
        "remote-model": _GraphModel(endpoint="remote"),
    }
    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: models[endpoint],
    )


def _route_async_clients(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport
) -> None:
    real_async_client = httpx.AsyncClient

    class _RoutedAsyncClient(real_async_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("transport", transport)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _RoutedAsyncClient)


def _all_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_all_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_all_text(item) for item in value)
    return ""


def _request_with_sender(
    client: TestClient,
    *,
    protocol: str,
    payload: dict[str, Any],
) -> _RequestResult:
    with mlflow.start_span(name=f"sender:{protocol}") as sender:
        headers = inject_tracing_headers(
            {
                "X-Forwarded-Access-Token": OBO_TOKEN,
                "X-Forwarded-Host": "caller.example",
            }
        )
    path = "/" if protocol == "a2a" else f"/{protocol}"
    return _RequestResult(
        response=client.post(path, json=payload, headers=headers),
        sender=sender,
    )


def _trace_for(trace_id: str, experiment_id: str) -> Any:
    traces = mlflow.search_traces(
        locations=[experiment_id],
        return_type="list",
        include_spans=True,
    )
    return next(trace for trace in traces if trace.info.trace_id == trace_id)


def _assert_real_parentage(trace: Any, sender: Any, protocol: str) -> None:
    spans = trace.data.spans
    assert any(span.span_id == sender.span_id for span in spans)
    route_name = "POST / (A2A)" if protocol == "a2a" else f"POST /{protocol}"
    request_span = next(span for span in spans if span.name == route_name)
    assert request_span.parent_id == sender.span_id
    remote_spans = [span for span in spans if span.name == "POST /responses"]
    assert any(
        span.parent_id is not None
        and span.parent_id != sender.span_id
        and any(parent.span_id == span.parent_id for parent in spans)
        for span in remote_spans
    )


def test_generated_declared_binding_runs_one_logical_graph_across_protocols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two real ASGI apps reconcile the declared graph claim with its effects."""
    DIRECT_CALLS.clear()
    REVIEW_CALLS.clear()
    REMOTE_CALLS.clear()
    REMOTE_TOKENS.clear()
    _patch_workspace_clients(monkeypatch)
    _patch_models(monkeypatch)
    monkeypatch.setenv("PRICING_APP_URL", CARD_URL)
    monkeypatch.setenv("DATABRICKS_APP_NAME", "caller")

    generated_dir = tmp_path / "generated"
    config = AgentConfig(
        name="declared-graph",
        model="caller-model",
        bindings={"pricing": "$PRICING_APP_URL"},
        agents={
            "review": {"type": "agent", "instructions": "Review the request."},
            "pricing": {"type": "agent", "instructions": "Price the request."},
        },
        root={"type": "sequential", "agents": ["review", "pricing"]},
    )
    generate_project(config, generated_dir)
    loaded = _load_agent_config(pyproject_path=generated_dir / "pyproject.toml")
    assert loaded is not None

    direct = DataAgent(
        "main",
        "capabilities",
        name="data",
        description="Answers direct local capability questions.",
        include_functions=False,
        tables={"facts": ["value STRING"]},
        extra_tools=[read_capability],
    )
    review = Agent(
        name="review",
        description="Reviews a request before pricing.",
    )
    pricing = Agent(name="pricing", description="Internal pricing specialist.")
    pricing_flow = SequentialAgent([review, pricing], name="pricing_flow")
    caller = RouterAgent(agents=[direct, pricing_flow])

    remote = Agent(name="pricing_service", tools=[approved_price])
    remote_app = create_app(
        remote,
        config=AgentConfig(name="pricing-service", model="remote-model"),
    )

    old_tracking_uri = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f"file://{tmp_path / 'mlruns'}")
    experiment = mlflow.set_experiment("declared-a2a-binding-reality")
    senders: dict[str, Any] = {}
    try:
        with TestClient(remote_app):
            transport = _RecordingASGITransport(remote_app)
            _route_async_clients(monkeypatch, transport)
            caller_app = create_app(caller, config=loaded)
            with TestClient(caller_app) as client:
                topology = build_topology(client.app.state.agent_context)
                labels = {node["label"] for node in topology["nodes"]}
                assert {"data", "pricing_flow", "review", "pricing"} <= labels

                direct_result = _request_with_sender(
                    client,
                    protocol="invocations",
                    payload={
                        "messages": [
                            {"role": "user", "content": "read the local capability"}
                        ]
                    },
                )
                direct_response = direct_result.response
                assert direct_response.status_code == 200, direct_response.text
                assert DIRECT_RESULT in _all_text(direct_response.json())
                assert DIRECT_CALLS == ["observed request"]
                assert not transport.requests

                requests = {
                    "invocations": {
                        "messages": [{"role": "user", "content": "price this request"}]
                    },
                    "responses": {
                        "input": [{"role": "user", "content": "price this request"}]
                    },
                    "a2a": {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "message/send",
                        "params": {
                            "message": {
                                "role": "user",
                                "parts": [
                                    {"kind": "text", "text": "price this request"}
                                ],
                                "messageId": "m1",
                            }
                        },
                    },
                }
                responses: dict[str, httpx.Response] = {}
                for protocol, payload in requests.items():
                    request_result = _request_with_sender(
                        client, protocol=protocol, payload=payload
                    )
                    response = request_result.response
                    assert response.status_code == 200, response.text
                    assert REMOTE_RESULT in _all_text(response.json())
                    responses[protocol] = response
                    senders[protocol] = request_result.sender

                topology_text = json.dumps(topology, sort_keys=True)
                card_text = client.get("/.well-known/agent.json").text
                generated_source = (generated_dir / "agent.py").read_text()
                visible = "\n".join((topology_text, card_text, generated_source))

                mlflow.flush_trace_async_logging()

                def _verify_observed_effects() -> None:
                    expect(len(responses)).satisfies(
                        lambda count: count == 3, "all three ingress protocols ran"
                    ).verify()
                    expect(REVIEW_CALLS).satisfies(
                        lambda calls: calls == ["observed request"] * 3,
                        "local review ran exactly once per remote route",
                    ).verify()
                    expect(REMOTE_CALLS).satisfies(
                        lambda calls: calls == ["ran"] * 3,
                        "declared internal leaf ran exactly once per remote route",
                    ).verify()
                    expect(REMOTE_TOKENS).satisfies(
                        lambda tokens: tokens == [OBO_TOKEN] * 3,
                        "OBO reached remote tool logic",
                    ).verify()
                    posts = [
                        request
                        for request in transport.requests
                        if request.method == "POST"
                    ]
                    expect(len(posts)).satisfies(
                        lambda count: count == 3, "one remote POST per remote route"
                    ).verify()
                    expect({request.url.path for request in posts}).satisfies(
                        lambda paths: paths == {"/responses"},
                        "declared leaf used the real Responses transport",
                    ).verify()
                    for protocol, sender in senders.items():
                        trace = _trace_for(sender.trace_id, experiment.experiment_id)
                        _assert_real_parentage(trace, sender, protocol)

                claim_vs_reality(
                    claimed_success=all(
                        response.is_success for response in responses.values()
                    ),
                    verifier=_verify_observed_effects,
                    claim_label="generated declared A2A graph",
                )

                assert OBO_TOKEN not in visible
                assert CARD_URL not in visible
                assert "$PRICING_APP_URL" not in visible
                assert "RemoteDatabricksAgent" not in visible
                assert all(
                    OBO_TOKEN not in response.text for response in responses.values()
                )
    finally:
        mlflow.set_tracking_uri(old_tracking_uri)
