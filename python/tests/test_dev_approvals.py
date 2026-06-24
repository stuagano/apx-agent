"""Tests for the dev-UI approval surface — /_apx/approvals routes.

Covers:
  1. ApprovalStore discovery: app.state.approval_store override, bare
     PolicyGate on agent._before_tool, and a compose()-d hook.
  2. GET /_apx/approvals returns pending requests with the fields the
     chat-UI banner renders (id, tool_name, arguments, reason).
  3. POST approve/deny resolve the request and the gate honors the
     decision on the agent's retry (the full end-to-end ASK loop at the
     HTTP layer).
  4. 404 behavior: unknown approval ID, no store configured.
  5. Conversations API failure surfaces a 503 + error payload instead of
     a silent empty list (history-panel stabilization).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import (
    AgentConfig,
    AgentContext,
    ApprovalRequired,
    FunctionPolicy,
    PolicyAction,
    PolicyGate,
    PolicyResult,
    compose,
)
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard


class _AgentStub:
    """Minimal stand-in for a BaseAgent carrying a before_tool hook.

    A real stub class (not MagicMock) so attribute access outside
    ``_before_tool`` fails loudly.
    """

    def __init__(self, before_tool) -> None:
        self._before_tool = before_tool


def _ask_gate() -> PolicyGate:
    """A gate whose single policy always returns ASK."""
    return PolicyGate([
        FunctionPolicy(
            lambda ev: PolicyResult(action=PolicyAction.ASK, reason="needs human"),
            name="always_ask",
        ),
    ])


def _make_app(agent: Any | None) -> FastAPI:
    config = AgentConfig(name="approval-test", model="claude-fake")
    card = AgentCard(name="approval-test", description="", skills=[])
    ctx = AgentContext(config=config, tools=[], card=card, agent=agent)  # type: ignore[arg-type]
    app = FastAPI()
    app.state.agent_context = ctx
    app.include_router(build_dev_ui_router())
    return app


async def _get_json(app: FastAPI, path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get(path)
    return r


async def _post(app: FastAPI, path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(path)
    return r


# ---------------------------------------------------------------------------
# Store discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_gate_no_store_returns_empty_list():
    app = _make_app(agent=None)
    r = await _get_json(app, "/_apx/approvals")
    assert r.status_code == 200
    # No PolicyGate anywhere → empty list, NOT an error: a gate-less
    # agent is a normal configuration.
    assert r.json() == []


@pytest.mark.asyncio
async def test_discovers_store_from_bare_gate():
    gate = _ask_gate()
    with pytest.raises(ApprovalRequired):
        gate("send_email", {"to": "bob@x.com"})

    app = _make_app(agent=_AgentStub(before_tool=gate))
    r = await _get_json(app, "/_apx/approvals")
    assert r.status_code == 200
    pending = r.json()
    # The pending request raised through the gate must be visible with
    # every field the approval banner renders.
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "send_email"
    assert pending[0]["arguments"] == {"to": "bob@x.com"}
    assert pending[0]["reason"] == "needs human"
    assert pending[0]["id"].startswith("appr-")


@pytest.mark.asyncio
async def test_discovers_store_through_composed_hook():
    gate = _ask_gate()
    with pytest.raises(ApprovalRequired):
        gate("run_sql", {"query": "DROP TABLE t"})

    # Production wiring composes guards with the gate — discovery must
    # walk compose()'s .callbacks to find it.
    def benign_guard(name, args):
        return None

    hook = compose(benign_guard, gate)
    app = _make_app(agent=_AgentStub(before_tool=hook))
    r = await _get_json(app, "/_apx/approvals")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["tool_name"] == "run_sql"


@pytest.mark.asyncio
async def test_app_state_store_takes_precedence():
    from apx_agent import ApprovalStore

    gate = _ask_gate()
    with pytest.raises(ApprovalRequired):
        gate("tool_a", {"x": 1})

    explicit = ApprovalStore()
    explicit.request("tool_b", {"y": 2}, reason="from explicit store")

    app = _make_app(agent=_AgentStub(before_tool=gate))
    app.state.approval_store = explicit
    r = await _get_json(app, "/_apx/approvals")
    # app.state.approval_store wins over the gate's private store —
    # tool_b (explicit) is listed, tool_a (gate) is not.
    names = [p["tool_name"] for p in r.json()]
    assert names == ["tool_b"]


# ---------------------------------------------------------------------------
# Approve / deny → gate honors the decision (end-to-end ASK loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_then_gate_allows_retry():
    gate = _ask_gate()
    args = {"to": "bob@x.com"}
    with pytest.raises(ApprovalRequired) as exc_info:
        gate("send_email", args)
    approval_id = exc_info.value.approval.id

    app = _make_app(agent=_AgentStub(before_tool=gate))
    r = await _post(app, f"/_apx/approvals/{approval_id}/approve")
    assert r.status_code == 200
    assert r.json() == {"id": approval_id, "status": "approved"}

    # The gate must now allow the identical retry — this is the claim
    # the whole approval surface exists for.
    gate("send_email", args)  # must not raise

    # And the pending list is empty again.
    r = await _get_json(app, "/_apx/approvals")
    assert r.json() == []


@pytest.mark.asyncio
async def test_deny_then_gate_blocks_retry():
    gate = _ask_gate()
    args = {"to": "eve@x.com"}
    with pytest.raises(ApprovalRequired) as exc_info:
        gate("send_email", args)
    approval_id = exc_info.value.approval.id

    app = _make_app(agent=_AgentStub(before_tool=gate))
    r = await _post(app, f"/_apx/approvals/{approval_id}/deny")
    assert r.status_code == 200
    assert r.json() == {"id": approval_id, "status": "denied"}

    # Retry must be a hard PermissionError (refused), not a re-ask.
    with pytest.raises(PermissionError, match="refused") as exc2:
        gate("send_email", args)
    assert not isinstance(exc2.value, ApprovalRequired)


@pytest.mark.asyncio
async def test_unknown_approval_id_404():
    app = _make_app(agent=_AgentStub(before_tool=_ask_gate()))
    r = await _post(app, "/_apx/approvals/appr-nope/approve")
    assert r.status_code == 404
    r = await _post(app, "/_apx/approvals/appr-nope/deny")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_approve_without_store_404():
    app = _make_app(agent=None)
    r = await _post(app, "/_apx/approvals/appr-x/approve")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Conversations API failure surfaces an error (history stabilization)
# ---------------------------------------------------------------------------


class _BrokenStore:
    """ConversationStore stub whose every method raises — simulates a
    misconfigured / unreachable backing store."""

    def list_conversations(self, **kwargs):
        raise RuntimeError("store exploded")

    def list_items(self, conv_id, **kwargs):
        raise RuntimeError("store exploded")


@pytest.mark.asyncio
async def test_broken_conversation_store_returns_503_not_empty_list():
    app = _make_app(agent=None)
    app.state.conversation_store = _BrokenStore()

    r = await _get_json(app, "/_apx/conversations")
    # A broken store must be distinguishable from "no conversations yet":
    # 503 + error payload, NOT a silent 200 [].
    assert r.status_code == 503
    assert "error" in r.json()

    r = await _get_json(app, "/_apx/conversations/conv-1/items")
    assert r.status_code == 503
    assert "error" in r.json()


@pytest.mark.asyncio
async def test_missing_conversation_store_still_returns_empty_list():
    # No store at all (e.g. memory-less scaffold) stays a friendly 200 []
    # — that's a normal configuration, not a failure.
    app = _make_app(agent=None)
    r = await _get_json(app, "/_apx/conversations")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# Chat UI template carries the approval + history-fix JS
# ---------------------------------------------------------------------------


def test_chat_template_includes_approval_and_history_js():
    from apx_agent._ui_chat import _render_agent_ui

    config = AgentConfig(name="t", model="m")
    card = AgentCard(name="t", description="", skills=[])
    ctx = AgentContext(config=config, tools=[], card=card, agent=None)  # type: ignore[arg-type]
    html = _render_agent_ui(ctx)
    # The approval banner functions must ship in the page.
    assert "checkPendingApprovals" in html
    assert "resolveApproval" in html
    assert "/_apx/approvals" in html
    # The history-desync fix: both flows must reset the request payload.
    assert "history.length = 0" in html


# ---------------------------------------------------------------------------
# PR-R2: trace + approval routes are un-hidden + typed, and the trace routes'
# HTML branch survives the added response_model.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r2_trace_and_approval_routes_published_to_openapi():
    spec = (await _get_json(_make_app(agent=None), "/openapi.json")).json()
    paths = spec["paths"]
    assert "get" in paths["/_apx/approvals"]
    assert "post" in paths["/_apx/approvals/{approval_id}/approve"]
    assert "post" in paths["/_apx/approvals/{approval_id}/deny"]
    assert "get" in paths["/_apx/traces"]
    # FastAPI renders the ``{trace_id:path}`` convertor as a plain ``{trace_id}``
    # path key in the schema.
    detail = next(p for p in paths if p.startswith("/_apx/traces/") and "{trace_id" in p)
    assert "get" in paths[detail]


@pytest.mark.asyncio
async def test_r2_traces_html_branch_survives_response_model():
    """The default (no ``fmt``) branch of ``GET /_apx/traces`` must still return
    an HTML page — adding ``response_model=list[TraceRow]`` documents only the
    ``fmt=json`` branch and must not hijack the HTMLResponse the handler
    returns."""
    async with AsyncClient(transport=ASGITransport(app=_make_app(agent=None)), base_url="http://t") as ac:
        r = await ac.get("/_apx/traces")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
