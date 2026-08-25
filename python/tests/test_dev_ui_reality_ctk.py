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

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ctk import claim_vs_reality, expect

from apx_agent import (
    AgentConfig,
    AgentContext,
    InMemoryConversationStore,
    InMemoryMemoryStore,
)
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


@pytest.mark.asyncio
async def test_chat_landing_route_renders_workflow_examples_once_and_escapes_text() -> None:
    config = AgentConfig(
        name="workflow-reality",
        examples=["What is the position?"],
        workflows=[{
            "id": "position",
            "title": "<Pricing review>",
            "question": "What is the position?",
            "purpose": "Compare <b>peers</b>.",
            "route": ["calibrate"],
        }],
    )
    card = AgentCard(name=config.name, description="", skills=[])
    app = FastAPI()
    app.state.agent_context = AgentContext(
        config=config, tools=[], card=card, agent=None  # type: ignore[arg-type]
    )
    app.include_router(build_dev_ui_router())

    async with _client(app) as ac:
        resp = await ac.get("/_apx/chat")

    assert resp.status_code == 200
    assert resp.text.count("What is the position?") == 1
    assert "&lt;Pricing review&gt;" in resp.text
    assert "Compare &lt;b&gt;peers&lt;/b&gt;." in resp.text
    assert "<b>peers</b>" not in resp.text


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


# ---------------------------------------------------------------------------
# 4) Memories — store a memory through the real store, read it back through
#    the route. (Previously only the memory TOOL had read-after-write coverage;
#    the GET /_apx/memories route had none.)
# ---------------------------------------------------------------------------


class _MemoryAgentStub:
    """Minimal stand-in for the served agent: the route reaches the memory
    store via ``ctx.agent._apx_memory_store`` (+ principal/namespace), exactly
    as :mod:`apx_agent._memory_wiring` attaches them at serve time."""

    def __init__(self, store: InMemoryMemoryStore, principal: str, namespace: str) -> None:
        self._apx_memory_store = store
        self._apx_memory_principal = principal
        self._apx_memory_namespace = namespace


def _ctx_with_agent(name: str, agent: object) -> AgentContext:
    config = AgentConfig(name=name, model="claude-fake")
    card = AgentCard(name=name, description="", skills=[])
    return AgentContext(config=config, tools=[], card=card, agent=agent)  # type: ignore[arg-type]


class TestMemoriesReadAfterWrite:
    """A memory written to the agent's ``_apx_memory_store`` must surface in
    ``GET /_apx/memories``. The route filters by the agent's principal +
    namespace via ``MemoryFilter``; a wrong scope, a dropped ``content`` field,
    or a serializer that silently returns ``[]`` would leave the landing-page
    preview blank. This writes a real memory and asserts the route hands the
    content back — the silent-failure shape (200 with ``[]``) fails here."""

    @pytest.mark.asyncio
    async def test_stored_memory_reads_back_through_route(self) -> None:
        principal, namespace = "alice", "profile"
        store = InMemoryMemoryStore()
        # WRITE: a real memory through the store the route reads from.
        store.add({
            "principal_id": principal,
            "namespace": namespace,
            "content": "prefers metric units",
        })

        app = FastAPI()
        app.state.agent_context = _ctx_with_agent(
            "mem-reality", _MemoryAgentStub(store, principal, namespace)
        )
        app.include_router(build_dev_ui_router())

        # READ: the route — the memory must round-trip with its content intact.
        async with _client(app) as ac:
            resp = await ac.get("/_apx/memories")

        assert resp.status_code == 200

        def _memory_reads_back() -> None:
            rows = resp.json()
            expect(rows, label="memory rows").nonempty() \
                .satisfies(lambda xs: all("content" in r for r in xs),
                           "every row carries content").verify()
            expect([r["content"] for r in rows], label="memory contents") \
                .contains("prefers metric units").verify()

        # claim_vs_reality: the store *claims* the add succeeded; the only
        # honest proof is the route handing the content back. A 200 with [] —
        # the silent-failure shape this test exists to catch — fails here.
        claim_vs_reality(
            claimed_success=True,
            verifier=_memory_reads_back,
            claim_label="memory add → /_apx/memories route",
        )


# ---------------------------------------------------------------------------
# 5) Eval cases — POST a list through the route, read it back through the GET
#    route (read-after-write through the real evals.json persistence), plus the
#    request-validation contract (malformed body → 422) the eval-route
#    hardening (PR-W1) introduced. These prove the WRITE slice: a 200 that
#    didn't actually persist, or validation that 500s instead of 422-ing, fails
#    here.
# ---------------------------------------------------------------------------


def _eval_app(name: str = "eval-reality") -> FastAPI:
    app = FastAPI()
    app.state.agent_context = _ctx(name)
    app.include_router(build_dev_ui_router())
    return app


class TestEvalDataReadAfterWrite:
    """A list POSTed to ``/_apx/eval/data`` must be readable back through
    ``GET /_apx/eval/data`` — through the real ``evals.json`` write, not a mock.
    A handler that returns ``{ok: true}`` without writing, or a GET that filters
    the persisted rows, fails here. The UI's divergent case fields
    (``expected_judge``, run metadata) must survive untouched: the request model
    uses ``extra="ignore"``, so this also guards that persistence re-reads the
    raw body rather than dumping the stripped models."""

    @pytest.mark.asyncio
    async def test_posted_cases_read_back_through_get_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "evals.json"
        # Hermetic: point the route's evals.json at a temp file (no agent_router
        # resolution, no workspace write — workspace_client is unset).
        monkeypatch.setattr("apx_agent._dev._find_evals_path", lambda: target)

        # Divergent shapes on purpose: one keyword case, one judge case carrying
        # run metadata that is NOT in the strict request model.
        cases = [
            {"question": "how many customers?", "expected": "42",
             "status": "pass", "response": "42", "trace_id": "tr-1"},
            {"question": "is the tone polite?", "expected_judge": "answer is courteous",
             "status": "pending", "response": "", "judge_verdict": None},
        ]

        app = _eval_app()
        async with _client(app) as ac:
            post_resp = await ac.post("/_apx/eval/data", json=cases)
            get_resp = await ac.get("/_apx/eval/data")

        assert post_resp.status_code == 200
        expect(post_resp.json(), label="save response") \
            .has_keys("ok", "count").verify()
        expect(post_resp.json()["count"], label="saved count").equals(2).verify()
        assert get_resp.status_code == 200

        def _round_trips() -> None:
            # The file the route persisted must hold the raw body verbatim
            # (divergent fields intact), and the GET route must hand it back.
            on_disk = json.loads(target.read_text())
            expect(on_disk, label="persisted evals.json").equals(cases).verify()
            rows = get_resp.json()
            expect(rows, label="GET eval rows").nonempty() \
                .satisfies(lambda xs: len(xs) == 2, "both cases persisted").verify()
            expect(rows, label="GET eval rows round-trip").equals(cases).verify()

        # claim_vs_reality: the POST *claims* it saved (200 {ok:true,count:2});
        # the only honest proof is the file holding the rows AND the GET route
        # reading them back. A 200 that wrote nothing fails here.
        claim_vs_reality(
            claimed_success=True,
            verifier=_round_trips,
            claim_label="eval POST → evals.json → GET round-trip",
        )

    @pytest.mark.asyncio
    async def test_malformed_body_is_rejected_with_422(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-list body is rejected by the request model (422) before the
        handler runs — and nothing is written to evals.json."""
        target = tmp_path / "evals.json"
        monkeypatch.setattr("apx_agent._dev._find_evals_path", lambda: target)

        app = _eval_app()
        async with _client(app) as ac:
            resp = await ac.post("/_apx/eval/data", json={"not": "a list"})

        assert resp.status_code == 422
        expect(target.exists(), label="evals.json after rejected save") \
            .equals(False).verify()


class TestEvalGetShapes:
    """Happy-path shape guards for the two read routes: the JSON list route and
    the HTML landing page. These ensure un-hiding the routes (PR-W1) did not
    break their existing return shapes."""

    @pytest.mark.asyncio
    async def test_eval_data_get_returns_json_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "evals.json"
        target.write_text(json.dumps([{"question": "q1", "expected": "a"}]))
        monkeypatch.setattr("apx_agent._dev._find_evals_path", lambda: target)

        app = _eval_app()
        async with _client(app) as ac:
            resp = await ac.get("/_apx/eval/data")

        assert resp.status_code == 200
        body = resp.json()
        expect(body, label="eval/data list").nonempty() \
            .satisfies(lambda xs: isinstance(xs, list), "is a JSON list").verify()
        expect(body[0]["question"], label="first case question").equals("q1").verify()

    @pytest.mark.asyncio
    async def test_eval_landing_renders_html(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "evals.json"
        target.write_text(json.dumps([{"question": "rendered question", "expected": "x"}]))
        monkeypatch.setattr("apx_agent._dev._find_evals_path", lambda: target)

        app = _eval_app()
        async with _client(app) as ac:
            resp = await ac.get("/_apx/eval")

        assert resp.status_code == 200
        expect(resp.headers["content-type"], label="eval landing content-type") \
            .contains("text/html").verify()
        expect(resp.text, label="eval landing body") \
            .contains("rendered question").verify()


class TestEvalJudgeContract:
    """The judge route's validation/503 contract — proven WITHOUT a live model
    (decision: never call a real LLM in the suite). A missing required field is
    a request-shape error (422); a valid body with no agent context is the
    semantic 503."""

    @pytest.mark.asyncio
    async def test_missing_required_field_is_422(self) -> None:
        app = FastAPI()
        # agent_context is read by direct attribute access in the handler; set
        # it explicitly so a (would-be) handler entry can't AttributeError into
        # a 500. Body validation 422s before the handler runs regardless.
        app.state.agent_context = None
        app.include_router(build_dev_ui_router())

        async with _client(app) as ac:
            resp = await ac.post("/_apx/eval/judge",
                                 json={"question": "Q?", "response": "x"})  # no criterion

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_valid_body_without_context_is_503(self) -> None:
        app = FastAPI()
        # Explicit None: the handler reads request.app.state.agent_context via
        # direct attribute access, so an unset State would AttributeError → 500
        # instead of the 503 this asserts.
        app.state.agent_context = None
        app.include_router(build_dev_ui_router())

        async with _client(app) as ac:
            resp = await ac.post("/_apx/eval/judge", json={
                "question": "Q?", "response": "x", "criterion": "y",
            })

        assert resp.status_code == 503
        expect(resp.json(), label="judge 503 body").has_keys("ok", "error").verify()
