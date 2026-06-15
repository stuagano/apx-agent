"""Read-after-write reality guards for the dev UI's three data surfaces.

The dev UI shows three things the agent produces at runtime: **conversations**
(multi-turn history), **traces** (each run, listed newest-first), and the
**events** inside a trace (tool calls + their progress messages). Every existing
test for these renders HTML from a hand-built payload or mocks the store — none
proves that something *written* through the real machinery reads back through
the real ``/_apx/*`` route. So a route that returns ``200`` with an empty body
(wrong filter, dropped field, serializer that silently omits events) sails
through the suite while the panel shows nothing in production.

These tests close that gap the ctk way: **write a real artifact, then read it
back through the actual route, and fail on empty.** Each is hermetic — the
conversation store is in-process, the trace list runs against a local-file
MLflow backend (never network/blob), and the trace detail serves from the
in-process ring buffer — so they run in CI with no infra.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ctk import claim_vs_reality, expect

from apx_agent import AgentConfig, AgentContext, InMemoryConversationStore
from apx_agent._conversation import MessageData, NewConversationItem
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard


def _ctx(name: str) -> AgentContext:
    config = AgentConfig(name=name, model="claude-fake")
    card = AgentCard(name=name, description="", skills=[])
    return AgentContext(config=config, tools=[], card=card, agent=None)  # type: ignore[arg-type]


def _user_msg(text: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="resp_reality",
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def _asst_msg(text: str, agent: str = "bot") -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="resp_reality",
        data=MessageData(
            role="assistant",
            agent=agent,
            content=[{"type": "output_text", "text": text}],
        ),
    )


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ---------------------------------------------------------------------------
# 1) Conversations — append through the store, read back through the route.
# ---------------------------------------------------------------------------


class TestConversationsReadAfterWrite:
    """A conversation written to the store must surface in BOTH the list route
    (which filters by ``agent_id = ctx.config.name``) and the items route (which
    returns the message content). A wrong agent_id filter or a serializer that
    drops ``data`` would make the history panel show an empty conversation —
    these assertions fail on exactly that."""

    @pytest.mark.asyncio
    async def test_appended_messages_read_back_through_routes(self) -> None:
        agent_name = "convo-reality"
        store = InMemoryConversationStore()
        # WRITE: a conversation bound to this agent + two real turns.
        conv = store.create_conversation(agent_id=agent_name, title="Reality check")
        store.append(conv.id, [_user_msg("how many customers?"),
                               _asst_msg("there are 42 customers")])

        app = FastAPI()
        app.state.agent_context = _ctx(agent_name)
        app.state.conversation_store = store
        app.include_router(build_dev_ui_router())

        # READ 1: the list route — the conversation must appear (agent_id filter
        # in the route matches the conversation's agent_id) with its title.
        async with _client(app) as ac:
            list_resp = await ac.get("/_apx/conversations")
            items_resp = await ac.get(f"/_apx/conversations/{conv.id}/items")

        assert list_resp.status_code == 200
        listed = list_resp.json()
        expect([c["id"] for c in listed], label="listed conversation ids") \
            .nonempty().contains(conv.id).verify()
        expect(next(c["title"] for c in listed if c["id"] == conv.id),
               label="conversation title").equals("Reality check").verify()

        # READ 2: the items route — both turns must round-trip, content intact.
        assert items_resp.status_code == 200
        body_text = items_resp.text

        def _both_turns_present() -> None:
            items = items_resp.json()
            expect(items, label="conversation items").nonempty() \
                .satisfies(lambda xs: len(xs) == 2, "both turns persisted").verify()
            expect(body_text, label="items payload") \
                .contains("how many customers?") \
                .contains("there are 42 customers").verify()

        # claim_vs_reality: the store *claims* the append succeeded; the only
        # honest proof is the route handing both messages back. A 200 with [] —
        # the silent-failure shape this test exists to catch — fails here.
        claim_vs_reality(
            claimed_success=True,
            verifier=_both_turns_present,
            claim_label="conversation append → items route",
        )

    @pytest.mark.asyncio
    async def test_items_route_isolates_by_conversation(self) -> None:
        """The items route must return only the requested conversation's items —
        a missing ``conv_id`` scope would bleed turns across sessions."""
        store = InMemoryConversationStore()
        a = store.create_conversation(agent_id="x", title="A")
        b = store.create_conversation(agent_id="x", title="B")
        store.append(a.id, [_user_msg("alpha question")])
        store.append(b.id, [_user_msg("bravo question")])

        app = FastAPI()
        app.state.agent_context = _ctx("x")
        app.state.conversation_store = store
        app.include_router(build_dev_ui_router())

        async with _client(app) as ac:
            resp = await ac.get(f"/_apx/conversations/{a.id}/items")

        assert resp.status_code == 200
        expect(resp.text, label="conv A items") \
            .contains("alpha question").not_matches("bravo question").verify()


# ---------------------------------------------------------------------------
# 2) Traces — log a real trace, read it back through the list route.
# ---------------------------------------------------------------------------


# Unmarked (not @pytest.mark.unit): exercises a REAL MLflow against a local file
# backend through the actual route — the opposite of "mocked boundaries".
class TestTracesListReadAfterWrite:
    """A trace produced by a run must appear in ``GET /_apx/traces?fmt=json``.
    The route reads via ``MlflowClient.search_traces(locations=...)`` — a kwarg
    drift (the ``experiment_names`` → ``locations`` history) or a backend that
    returns nothing would leave the Traces panel blank. This logs one real
    trace and asserts the route lists it."""

    @pytest.mark.asyncio
    async def test_logged_trace_appears_in_list_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mlflow

        from apx_agent import _trace_store as ts

        # Hermetic: local file backend, fresh ring buffer (the route merges
        # buffer entries into the list — reset so no prior test leaks in).
        ts.reset()
        mlflow.set_tracking_uri(f"file://{tmp_path}")
        exp_id = mlflow.create_experiment(f"apx-traces-{tmp_path.name}")
        mlflow.set_experiment(experiment_id=exp_id)
        # The route reads MLFLOW_EXPERIMENT_ID; pin it so it queries exactly this
        # experiment (and never falls back to scanning every experiment).
        monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", exp_id)

        # WRITE: one real trace, then flush (MLflow logs traces async).
        with mlflow.start_span(name="agent_run") as span:
            span.set_inputs({"q": "how many customers?"})
            span.set_outputs({"a": "42"})
        mlflow.flush_trace_async_logging()
        trace_id = mlflow.get_last_active_trace_id()
        assert trace_id, "no trace id captured from the logged span"

        app = FastAPI()
        app.state.agent_context = _ctx("traces-reality")
        app.include_router(build_dev_ui_router())

        async with _client(app) as ac:
            resp = await ac.get("/_apx/traces?fmt=json")

        assert resp.status_code == 200

        def _trace_is_listed() -> None:
            rows = resp.json()
            expect(rows, label="trace rows").nonempty().verify()
            expect([r["trace_id"] for r in rows], label="listed trace ids") \
                .contains(trace_id).verify()

        claim_vs_reality(
            claimed_success=True,
            verifier=_trace_is_listed,
            claim_label="logged trace → /_apx/traces list route",
        )


# ---------------------------------------------------------------------------
# 3) Events — a tool span's event must survive the trace-detail round-trip.
# ---------------------------------------------------------------------------


class TestTraceEventReadAfterWrite:
    """Tool progress events live on a span's ``events`` and are what make the
    Events panel useful (e.g. "Starting SQL warehouse — cold-start ~20-30s").
    The ring buffer feeds the detail route directly; this writes a span carrying
    a tool event and asserts the route hands the **event payload** back — not
    merely that the span exists (that's already covered elsewhere)."""

    @pytest.mark.asyncio
    async def test_tool_event_round_trips_through_detail_route(self) -> None:
        from apx_agent import _trace_store as ts

        ts.reset()
        # WRITE: a TOOL span carrying a progress event, into the ring buffer
        # (the FEVM/private-link path the detail route serves from on a hit).
        ts.put("tr-events", [{
            "span_id": "s1", "parent_id": None, "name": "run_sql",
            "span_type": "TOOL", "status": "OK",
            "start_time_ns": 0, "end_time_ns": 1_000_000, "duration_ms": 1.0,
            "inputs": {"query": "SELECT count(*) FROM customers"}, "outputs": None,
            "events": [{
                "name": "apx.progress",
                "attributes": {"message": "Starting SQL warehouse — cold-start ~20-30s"},
            }],
        }])

        app = FastAPI()
        app.state.agent_context = _ctx("events-reality")
        app.include_router(build_dev_ui_router())

        async with _client(app) as ac:
            resp = await ac.get("/_apx/traces/tr-events?fmt=json")

        assert resp.status_code == 200

        def _event_payload_present() -> None:
            body = resp.json()
            expect(body, label="trace detail").has_keys("trace_id", "spans").verify()
            spans = body["spans"]
            expect(spans, label="detail spans").nonempty().verify()
            events = spans[0].get("events") or []
            expect(events, label="span events").nonempty().verify()
            expect(events[0]["attributes"]["message"], label="event message") \
                .contains("Starting SQL warehouse").verify()

        claim_vs_reality(
            claimed_success=True,
            verifier=_event_payload_present,
            claim_label="tool event → /_apx/traces/{id} detail route",
        )
