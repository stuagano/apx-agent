"""Ctk reality proof for a generated, declaratively bound internal A2A leaf."""

from __future__ import annotations

import json
import time
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


CARD_URL = "http://pricing.internal/.well-known/agent.json"
TEST_OBO_SENTINEL = "NON_SECRET_TEST_OBO"
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


def review_request(question: str) -> str:
    """Review locally before invoking the bound pricing leaf."""
    REVIEW_CALLS.append(question)
    return "reviewed"


def approved_price(headers: Dependencies.Headers) -> str:
    """Return a deterministic price while observing the caller identity."""
    from apx_agent._mlflow_tracing import emit_progress

    REMOTE_CALLS.append("ran")
    REMOTE_TOKENS.append(
        headers.token.get_secret_value() if headers.token is not None else None
    )
    emit_progress("pricing", logical_agent="pricing")
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
                name
                for name in ("read_capability", "review_request", "approved_price")
                if name in names
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
        export_barrier = mlflow.start_span_no_context(
            "receiver-export-barrier", parent_span=sender
        )
        headers = inject_tracing_headers(
            {
                "X-Forwarded-Access-Token": TEST_OBO_SENTINEL,
                "X-Forwarded-Host": "caller.example",
            }
        )
    # The sender is closed before ingress, while a non-active child keeps its
    # trace export open until the receiver's spans have finished. This avoids a
    # local-store race without making the barrier the receiver's parent.
    path = "/" if protocol == "a2a" else f"/{protocol}"
    try:
        response = client.post(path, json=payload, headers=headers)
    finally:
        export_barrier.end()
    mlflow.flush_trace_async_logging()
    return _RequestResult(response=response, sender=sender)


def _protocol_payload(protocol: str, query: str, *, message_id: str) -> dict[str, Any]:
    if protocol == "invocations":
        return {"messages": [{"role": "user", "content": query}]}
    if protocol == "responses":
        return {"input": [{"role": "user", "content": query}]}
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": query}],
                "messageId": message_id,
            }
        },
    }


def _post_count(transport: _RecordingASGITransport) -> int:
    return sum(request.method == "POST" for request in transport.requests)


def _eventually_complete_trace(
    sender: Any,
    experiment_id: str,
    protocol: str,
    *,
    timeout_seconds: float = 8.0,
) -> Any:
    """Wait briefly for async MLflow export to persist the complete trace."""
    route_name = "POST / (A2A)" if protocol == "a2a" else f"POST /{protocol}"
    deadline = time.monotonic() + timeout_seconds
    observed: list[dict[str, Any]] = []
    while True:
        traces = mlflow.search_traces(
            locations=[experiment_id],
            return_type="list",
            include_spans=True,
            flush=True,
        )
        trace = next(
            (item for item in traces if item.info.trace_id == sender.trace_id), None
        )
        if trace is not None:
            spans = trace.data.spans
            observed = [
                {
                    "name": span.name,
                    "parent_id": span.parent_id,
                    "agent_name": (span.attributes or {}).get("apx.agent.name"),
                    "events": [event.name for event in (span.events or [])],
                }
                for span in spans
            ]
            has_caller = any(
                span.name == route_name
                and (span.attributes or {}).get("apx.agent.name") == "declared-graph"
                for span in spans
            )
            has_remote = any(
                span.name == "POST /responses"
                and (span.attributes or {}).get("apx.agent.name") == "pricing-service"
                for span in spans
            )
            has_progress = any(
                event.name == "apx.progress"
                for span in spans
                for event in (span.events or [])
            )
            if has_caller and has_remote and has_progress:
                return trace
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"trace {sender.trace_id} incomplete for {protocol} after "
                f"{timeout_seconds:.1f}s; observed={observed!r}"
            )
        time.sleep(0.05)


def _assert_real_parentage(trace: Any, sender: Any, protocol: str) -> list[Any]:
    spans = trace.data.spans
    assert any(span.span_id == sender.span_id for span in spans)
    route_name = "POST / (A2A)" if protocol == "a2a" else f"POST /{protocol}"
    request_span = next(
        span
        for span in spans
        if span.name == route_name
        and (span.attributes or {}).get("apx.agent.name") == "declared-graph"
    )
    assert request_span.parent_id == sender.span_id
    remote_span = next(
        span
        for span in spans
        if span.name == "POST /responses"
        and (span.attributes or {}).get("apx.agent.name") == "pricing-service"
    )

    # MLflow's LangGraph autologging inserts `_sync_node` between the caller
    # ingress and the HTTP continuation. Without autolog the served path parent
    # is `graph.invoke` from safe_span("graph.invoke"). Prove the exact
    # receiver identity, its immediate parent, and the complete parent chain
    # back to this protocol's caller request span.
    by_id = {span.span_id: span for span in spans}
    parent = by_id[remote_span.parent_id]
    assert parent.name in {"_sync_node", "graph.invoke"}
    remote_traceparent = (remote_span.attributes or {})["apx.traceparent"]
    assert remote_traceparent.split("-")[2] == remote_span.parent_id
    ancestor_ids: list[str] = []
    ancestor_names: list[str] = []
    while parent is not None:
        ancestor_ids.append(parent.span_id)
        ancestor_names.append(parent.name)
        if parent.span_id == request_span.span_id:
            break
        parent = by_id.get(parent.parent_id)
    assert request_span.span_id in ancestor_ids
    # Autolog inserts a node named "pricing"; without it the served path is graph.invoke.
    assert "pricing" in ancestor_names or "graph.invoke" in ancestor_names

    return [
        event
        for span in spans
        for event in (span.events or [])
        if event.name == "apx.progress"
    ]


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
        tools=[review_request],
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
    traces_by_protocol: dict[str, Any] = {}
    route_observations: list[dict[str, Any]] = []
    try:
        with TestClient(remote_app):
            pass
        transport = _RecordingASGITransport(remote_app)
        _route_async_clients(monkeypatch, transport)
        caller_app = create_app(caller, config=loaded)
        with TestClient(caller_app) as client:
            topology_response = client.get("/_apx/topology.json")
            card_response = client.get("/.well-known/agent.json")
            responses: dict[str, httpx.Response] = {}
            all_responses: list[httpx.Response] = []
            for protocol in ("invocations", "responses", "a2a"):
                before = {
                    "direct": len(DIRECT_CALLS),
                    "review": len(REVIEW_CALLS),
                    "remote": len(REMOTE_CALLS),
                    "posts": _post_count(transport),
                }
                direct_result = _request_with_sender(
                    client,
                    protocol=protocol,
                    payload=_protocol_payload(
                        protocol,
                        "read the local capability",
                        message_id=f"{protocol}-direct",
                    ),
                )
                direct_response = direct_result.response
                assert direct_response.status_code == 200, direct_response.text
                assert DIRECT_RESULT in _all_text(direct_response.json())
                direct_delta = {
                    "protocol": protocol,
                    "route": "direct",
                    "direct": len(DIRECT_CALLS) - before["direct"],
                    "review": len(REVIEW_CALLS) - before["review"],
                    "remote": len(REMOTE_CALLS) - before["remote"],
                    "posts": _post_count(transport) - before["posts"],
                }
                assert direct_delta == {
                    "protocol": protocol,
                    "route": "direct",
                    "direct": 1,
                    "review": 0,
                    "remote": 0,
                    "posts": 0,
                }
                route_observations.append(direct_delta)
                all_responses.append(direct_response)

                before = {
                    "direct": len(DIRECT_CALLS),
                    "review": len(REVIEW_CALLS),
                    "remote": len(REMOTE_CALLS),
                    "posts": _post_count(transport),
                }
                request_result = _request_with_sender(
                    client,
                    protocol=protocol,
                    payload=_protocol_payload(
                        protocol,
                        "price this request",
                        message_id=f"{protocol}-pricing",
                    ),
                )
                response = request_result.response
                assert response.status_code == 200, response.text
                assert REMOTE_RESULT in _all_text(response.json())
                remote_delta = {
                    "protocol": protocol,
                    "route": "pricing",
                    "direct": len(DIRECT_CALLS) - before["direct"],
                    "review": len(REVIEW_CALLS) - before["review"],
                    "remote": len(REMOTE_CALLS) - before["remote"],
                    "posts": _post_count(transport) - before["posts"],
                }
                assert remote_delta == {
                    "protocol": protocol,
                    "route": "pricing",
                    "direct": 0,
                    "review": 1,
                    "remote": 1,
                    "posts": 1,
                }
                route_observations.append(remote_delta)
                responses[protocol] = response
                all_responses.append(response)
                senders[protocol] = request_result.sender
                traces_by_protocol[protocol] = _eventually_complete_trace(
                    request_result.sender,
                    experiment.experiment_id,
                    protocol,
                )

            generated_source = (generated_dir / "agent.py").read_text()

            mlflow.flush_trace_async_logging()

            def _verify_observed_effects() -> None:
                assert topology_response.status_code == 200, topology_response.text
                topology = topology_response.json()
                labels = {node["label"] for node in topology["nodes"]}
                assert {"data", "pricing_flow", "review", "pricing"} <= labels
                assert card_response.status_code == 200, card_response.text
                card = card_response.json()
                assert card["name"] == "declared-graph"
                card_skill_names = {
                    skill["name"] for skill in card.get("skills", [])
                }
                assert "read_capability" in card_skill_names
                expect(len(responses)).satisfies(
                    lambda count: count == 3, "all three ingress protocols ran"
                ).verify()
                expect(route_observations).satisfies(
                    lambda observations: len(observations) == 6,
                    "both router branches ran through every ingress protocol",
                ).verify()
                for observation in route_observations:
                    expected = (
                        {"direct": 1, "review": 0, "remote": 0, "posts": 0}
                        if observation["route"] == "direct"
                        else {"direct": 0, "review": 1, "remote": 1, "posts": 1}
                    )
                    assert {
                        key: observation[key]
                        for key in ("direct", "review", "remote", "posts")
                    } == expected
                expect(REVIEW_CALLS).satisfies(
                    lambda calls: calls == ["observed request"] * 3,
                    "local review ran exactly once per remote route",
                ).verify()
                expect(REMOTE_CALLS).satisfies(
                    lambda calls: calls == ["ran"] * 3,
                    "declared internal leaf ran exactly once per remote route",
                ).verify()
                expect(REMOTE_TOKENS).satisfies(
                    lambda tokens: tokens == [TEST_OBO_SENTINEL] * 3,
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
                for request in posts:
                    items = json.loads(request.content)["input"]
                    call = next(item for item in items if item.get("type") == "function_call")
                    result = next(item for item in items if item.get("type") == "function_call_output")
                    assert call["name"] == "review_request"
                    assert json.loads(call["arguments"]) == {"question": "observed request"}
                    assert call["call_id"] == result["call_id"] == "review_request-1"
                    assert result["output"] == "reviewed"
                progress_events: list[Any] = []
                for protocol, sender in senders.items():
                    trace = traces_by_protocol[protocol]
                    events = _assert_real_parentage(trace, sender, protocol)
                    assert len(events) == 1
                    assert (events[0].attributes or {}).get("message") == "pricing"
                    assert (events[0].attributes or {}).get("logical_agent") == (
                        "pricing"
                    )
                    progress_events.extend(events)

                progress_text = json.dumps(
                    [dict(event.attributes or {}) for event in progress_events],
                    sort_keys=True,
                )
                safe_trace_attribute_names = {
                    "http.route",
                    "apx.a2a_method",
                    "apx.agent.name",
                    "apx.input_items",
                    "apx.message_count",
                    "apx.streaming",
                    "apx.traceparent",
                    "apx.user_scoped",
                }
                trace_text = json.dumps(
                    {
                        protocol: [
                            {
                                "name": span.name,
                                "attributes": {
                                    key: value
                                    for key, value in (span.attributes or {}).items()
                                    if key in safe_trace_attribute_names
                                },
                            }
                            for span in trace.data.spans
                        ]
                        for protocol, trace in traces_by_protocol.items()
                    },
                    sort_keys=True,
                )
                response_text = "\n".join(
                    response.text for response in all_responses
                )
                visible = "\n".join(
                    (
                        topology_response.text,
                        card_response.text,
                        generated_source,
                        progress_text,
                        trace_text,
                        response_text,
                    )
                )
                for forbidden in (
                    TEST_OBO_SENTINEL,
                    CARD_URL,
                    "$PRICING_APP_URL",
                    "RemoteDatabricksAgent",
                ):
                    assert forbidden not in visible

            claim_vs_reality(
                claimed_success=all(
                    response.is_success
                    for response in [
                        topology_response,
                        card_response,
                        *all_responses,
                    ]
                ),
                verifier=_verify_observed_effects,
                claim_label="generated declared A2A graph",
            )
    finally:
        try:
            mlflow.flush_trace_async_logging()
        finally:
            mlflow.set_tracking_uri(old_tracking_uri)


@pytest.mark.parametrize(
    "protocol,stream",
    [("invocations", False), ("responses", False), ("a2a", False),
     ("invocations", True), ("responses", True)],
)
def test_required_bound_leaf_failure_is_opaque_at_ingress(
    monkeypatch: pytest.MonkeyPatch, protocol: str, stream: bool
) -> None:
    _patch_workspace_clients(monkeypatch)
    _patch_models(monkeypatch)

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"name": "private", "url": "http://pricing.internal"})
        return httpx.Response(502, text=f"NON_SECRET_UPSTREAM_BODY {CARD_URL}")

    _route_async_clients(monkeypatch, httpx.MockTransport(upstream))
    caller = Agent(name="pricing")
    app = create_app(
        caller,
        config=AgentConfig(
            name="failure-proof", model="caller-model", bindings={"pricing": CARD_URL}
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        payload = _protocol_payload(protocol, "price", message_id="failure-proof")
        if stream:
            payload["stream"] = True
        response = client.post(
            "/" if protocol == "a2a" else f"/{protocol}",
            json=payload,
        )
    if protocol == "a2a":
        assert response.json()["result"]["status"]["state"] == "failed"
    elif stream:
        assert '"error"' in response.text
    else:
        assert response.status_code == 500
    if protocol == "a2a" or stream:
        assert "Stage 'pricing' failed" in response.text
    assert CARD_URL not in response.text
    assert "NON_SECRET_UPSTREAM_BODY" not in response.text
