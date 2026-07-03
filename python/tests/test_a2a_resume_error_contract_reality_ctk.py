"""Claim-vs-reality: a failing A2A resume must not break the JSON-RPC contract (#374).

In `_run_message_send`, `chat_agent.thread_interrupt(context_id, custom_inputs)`
was called OUTSIDE the try/except that converts turn errors into a failed Task.
thread_interrupt compiles the graph, resolves OBO (can raise ApxIdentityError
from the fail-closed guard), and reads checkpointer state (can raise on a
Lakebase/get_state error). So a resume against a Lakebase-backed agent that hit
a transient checkpointer error — or ran with no OBO token — propagated as a raw
FastAPI HTTP 500 instead of a JSON-RPC error / failed Task, breaking the
client's contract parsing.

This reconciles the *claim* ("message/send always returns a JSON-RPC response")
against reality: when the resume path raises, the client gets a 200 JSON-RPC
response carrying a failed Task (or a JSON-RPC error) — never a raw 500.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from apx_agent import AgentConfig, LlmAgent, create_app


def _trivial_tool(query: str) -> str:
    """Echo."""
    return query


@pytest.fixture
def a2a_client_and_agent():
    agent = LlmAgent(tools=[_trivial_tool])
    config = AgentConfig(name="a2a-test", model="databricks-claude-sonnet-4-6")
    captured: dict[str, Any] = {}
    from apx_agent import _chat_agent as _ca_module

    original = _ca_module.chat_agent_for

    def _spy(agent_arg, *, model, conversation_store=None, agent_id=None, checkpointer=None):
        ca = original(agent_arg, model=model)
        captured["chat_agent"] = ca
        ca.predict = MagicMock(name="predict")
        return ca

    with patch.object(_ca_module, "chat_agent_for", side_effect=_spy), patch(
        "apx_agent._wiring._make_workspace_client"
    ) as mock_ws:
        mock_ws.return_value = MagicMock(name="ws")
        app = create_app(agent, config=config)
        # raise_server_exceptions=False so an unhandled 500 is observable as a
        # response (the bug) instead of re-raised into the test.
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, captured


def _resume_body() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "approve it"}],
                "messageId": "u1",
                "contextId": "ctx-1",
            },
            "configuration": {"apx_resume": "approve"},
        },
    }


@pytest.mark.unit
def test_resume_thread_interrupt_error_is_json_rpc_not_500(a2a_client_and_agent) -> None:
    client, captured = a2a_client_and_agent
    captured["chat_agent"].thread_interrupt = MagicMock(
        side_effect=RuntimeError("checkpointer read failed")
    )

    resp = client.post("/", json=_resume_body())

    assert resp.status_code == 200, f"resume error leaked as HTTP {resp.status_code}, not JSON-RPC"
    body = resp.json()
    assert body.get("jsonrpc") == "2.0"
    # Either a JSON-RPC error object, or a success carrying a failed Task.
    is_error = "error" in body
    is_failed_task = (
        not is_error and body.get("result", {}).get("status", {}).get("state") == "failed"
    )
    assert is_error or is_failed_task, f"resume error not surfaced per contract: {body}"
