"""Native chat mounts must retain the SDK invocation and identity boundary."""

import pytest

from tests.test_native_invocation_contract import native, request_body  # noqa: F401
from tests.test_runtime_targets import execution  # noqa: F401


def test_native_chat_mounts_without_legacy_handlers(native):
    assert native.client.get("/").status_code == 200
    assert "apxNativeChat" in native.client.get("/").text
    assert native.client.post("/invocations", json={}).status_code == 404
    assert native.client.post("/api/invocations", json=request_body()).status_code == 401
    # Apps do not enable the developer surface implicitly.
    assert native.client.get("/_apx/chat").status_code == 404
    assert native.client.get("/api/input-schema").json() is None
    assert native.client.post("/api/input-validation", json={}).status_code == 422


def test_native_chat_renderers_select_transport_explicitly():
    from apx_agent._ui_chat import _render_agent_ui
    from apx_agent._ui_root_chat import render_root_chat

    for render in [lambda **kw: _render_agent_ui(None, **kw), render_root_chat]:
        assert "apxNativeChat" not in render()
        assert "apxNativeChat" in render(native=True)


def test_native_declared_input_discovery_and_validation_without_execution():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from apx_agent import LlmAgent, SequentialAgent
    from apx_agent._native_input import native_data_part
    from apx_agent._ui_native_chat import native_input_router
    from tests.test_native_remote_invocations import DomainInput

    agent = SequentialAgent([LlmAgent(input_schema=DomainInput, input_key="domain")])
    app = FastAPI()
    app.include_router(native_input_router(agent))
    with TestClient(app) as client:
        assert client.get("/api/input-schema").json() == DomainInput.model_json_schema()
        assert client.post("/api/input-validation", json={"amount": 4}).json() == native_data_part(DomainInput, {"amount": 4})
        for payload in [{}, {"amount": "4"}, {"amount": 4, "principal": "admin"}]:
            response = client.post("/api/input-validation", json=payload)
            assert response.status_code == 422
            assert response.json()["detail"]
        assert "amount" in client.post("/api/input-validation", json={}).json()["detail"]
        assert client.post("/api/input-validation", content="x" * 65537).status_code == 413


def test_native_untyped_discovery_does_not_accept_typed_payload():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from apx_agent import LlmAgent
    from apx_agent._ui_native_chat import native_input_router

    app = FastAPI()
    app.include_router(native_input_router(LlmAgent()))
    with TestClient(app) as client:
        assert client.get("/api/input-schema").json() is None
        assert client.post("/api/input-validation", json={}).status_code == 422


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_native_dev_chat_enablement(request, monkeypatch, enabled):
    monkeypatch.setenv("APX_DEV_UI", enabled)
    host = request.getfixturevalue("native")
    response = host.client.get("/_apx/chat")
    assert response.status_code == (200 if enabled == "1" else 404)
    if enabled == "1":
        assert "apxNativeChat" in response.text
        assert 'src="/_apx/vendor/' not in response.text
    assert host.client.post("/invocations", json={}).status_code == 404
    assert host.client.post("/_apx/eval/judge", json={}).status_code == 404
