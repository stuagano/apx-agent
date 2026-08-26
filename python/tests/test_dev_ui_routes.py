"""Route + nav-link health for the dev UI.

Guards against nav drift: every link the nav bar advertises must resolve to a
real, renderable page (not a 404 or a redirect to a removed page), and no page
may advertise a link outside the canonical set. This is the automated answer to
"how do we check that all those links work" — it runs in CI on every change.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import AgentConfig, AgentContext
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard
from apx_agent._ui_nav import APX_NAV_PAGES


def _make_ctx() -> AgentContext:
    config = AgentConfig(name="nav-test", model="claude-fake")
    card = AgentCard(name="nav-test", description="", skills=[])
    return AgentContext(config=config, tools=[], card=card, agent=None)  # type: ignore[arg-type]


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.state.agent_context = _make_ctx()
    a.include_router(build_dev_ui_router())
    return a


CANONICAL_SLUGS = {slug for slug, _ in APX_NAV_PAGES}
# Pages whose nav bar we render server-side and want to audit for dead links.
NAV_PAGE_SLUGS = ["agent", "edit", "setup", "eval", "probe"]


class TestCanonicalNavRoutesResolve:
    """Every canonical nav link must land on a real page (HTTP 200)."""

    @pytest.mark.parametrize("slug", sorted(CANONICAL_SLUGS))
    @pytest.mark.asyncio
    async def test_route_resolves_to_page(self, app: FastAPI, slug: str):
        # Follow redirects — what the browser actually experiences. Some pages
        # intentionally 302 (e.g. to a trailing-slash variant so an SPA's
        # relative asset paths resolve).
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get(f"/_apx/{slug}", follow_redirects=True)
        assert r.status_code == 200, (
            f"/_apx/{slug} resolved to {r.status_code} — a nav link points at a "
            f"page that no longer renders. Update APX_NAV_PAGES or restore the route."
        )
        assert r.text.strip(), f"/_apx/{slug} returned an empty body"


class TestNoDeadNavLinks:
    """No rendered page may advertise a link outside the canonical set."""

    @pytest.mark.parametrize("slug", NAV_PAGE_SLUGS)
    @pytest.mark.asyncio
    async def test_page_nav_links_are_canonical(self, app: FastAPI, slug: str):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get(f"/_apx/{slug}")
        assert r.status_code == 200
        # Pull every /_apx/<word> nav target out of the rendered HTML.
        linked = set(re.findall(r'href="/_apx/([a-z]+)"', r.text))
        # The page may link to non-nav destinations (traces, chat). Only assert
        # that it does NOT link to known-removed pages.
        removed = {"tools", "wizard", "builder"}
        dead = linked & removed
        assert not dead, (
            f"/_apx/{slug} still links to removed page(s) {dead}. "
            f"Render the nav via _apx_nav_links() so it stays canonical."
        )


class TestStandardEndpointLinks:
    """The dev shell surfaces the agent's standard endpoints (A2A card, API spec,
    health, readiness) so they're reachable from the UI — previously invisible."""

    @pytest.mark.asyncio
    async def test_agent_shell_links_standard_endpoints(self, app: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/agent")
        assert r.status_code == 200
        for path in ("/.well-known/agent.json", "/_apx/openapi.json", "/health", "/readyz"):
            assert f'href="{path}"' in r.text, (
                f"Endpoints menu is missing a link to {path}"
            )

    @pytest.mark.asyncio
    async def test_openapi_endpoint_link_is_not_dead(self, app: FastAPI):
        # The one standard link the dev router itself mounts — prove it resolves
        # (the others are covered by the protocol/readyz suites).
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/openapi.json")
        assert r.status_code == 200


class TestLegacyRedirects:
    """Removed pages still redirect somewhere valid (no hard 404)."""

    @pytest.mark.parametrize("slug,target", [("tools", "/_apx/edit"), ("wizard", "/_apx/setup")])
    @pytest.mark.asyncio
    async def test_legacy_route_redirects(self, app: FastAPI, slug: str, target: str):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get(f"/_apx/{slug}", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert r.headers["location"].startswith(target)


class TestVendorAssets:
    """Vendored markdown libs (marked + DOMPurify) are served locally from
    /_apx/vendor/ so the deployed app needs no CDN (offline/private-link safe)."""

    @pytest.mark.asyncio
    async def test_serves_marked(self, app: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/vendor/marked.min.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]
        assert len(r.content) > 1000

    @pytest.mark.asyncio
    async def test_serves_purify(self, app: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/vendor/purify.min.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]
        assert len(r.content) > 1000

    @pytest.mark.asyncio
    async def test_vendor_path_traversal_blocked(self, app: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/vendor/../topology/index.html")
        assert r.status_code in (403, 404)


class TestMarkdownWiring:
    """The chat page loads the vendored libs locally and renders assistant
    messages as sanitized markdown."""

    def test_page_loads_vendor_libs_and_renders_assistant(self):
        from apx_agent._ui_chat import _render_agent_ui

        # Building the full HTML also catches f-string brace-doubling mistakes
        # in the CSS/JS we added (a stray single {/} throws at f-string eval).
        html = _render_agent_ui(_make_ctx())
        assert "/_apx/vendor/marked.min.js" in html
        assert "/_apx/vendor/purify.min.js" in html
        assert "DOMPurify.sanitize(marked.parse(" in html  # render wiring present
        assert "renderAssistantInto" in html  # helper present

    def test_dev_ui_sends_thread_id_in_custom_inputs(self):
        """Dev UI must pass custom_inputs.thread_id with every /responses call
        so the server-side session store is used (multi-turn memory, reload
        resume). The thread_id is generated once per sessionStorage lifetime."""
        from apx_agent._ui_chat import _render_agent_ui

        html = _render_agent_ui(_make_ctx())
        # sessionStorage persistence so page refresh resumes the same session.
        assert "sessionStorage" in html
        assert "_apx_dev_thread_id" in html
        assert "devThreadId" in html
        # The main chat fetch must include custom_inputs carrying the thread_id.
        assert "custom_inputs" in html
        assert "thread_id: devThreadId" in html

    def test_embed_mode_keeps_shared_chat_but_removes_page_chrome(self):
        from apx_agent._ui_chat import _render_agent_ui

        html = _render_agent_ui(_make_ctx(), embed=True)
        assert '<body class="apx-embed">' in html
        assert "body.apx-embed header" in html
        assert "body.apx-embed #landing" in html
        assert "body.apx-embed .main" in html
        assert "body.apx-embed .right-panel" in html
        assert '<button class="active" onclick="switchTab(\'trace\',this)">Trace</button>' in html
        assert '<div id="tab-trace" class="tab-panel active">' in html
        assert "stream: true" in html
        assert "thread_id: devThreadId" in html


# ---------------------------------------------------------------------------
# _pick_workspace_defaults — the Setup page's auto-prefill source
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from apx_agent._dev import _pick_workspace_defaults


def _named(name: str, **extra) -> SimpleNamespace:
    return SimpleNamespace(name=name, **extra)


def _wh(wh_id: str, state: str, serverless: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=wh_id,
        state=SimpleNamespace(value=state),
        enable_serverless_compute=serverless,
    )


class _MockWs:
    def __init__(self, *, catalogs, schemas_by_catalog, warehouses):
        self._catalogs = catalogs
        self._schemas = schemas_by_catalog
        self._warehouses = warehouses
        self.catalogs = SimpleNamespace(list=lambda: list(self._catalogs))
        self.schemas = SimpleNamespace(
            list=lambda catalog_name: list(self._schemas.get(catalog_name, []))
        )
        self.warehouses = SimpleNamespace(list=lambda: list(self._warehouses))


class TestPickWorkspaceDefaults:
    """Auto-prefill picks sensible defaults instead of forcing the user to
    manually pick catalog + schema + warehouse on every fresh agent."""

    def test_prefers_user_catalog_over_samples(self):
        """A real user catalog wins over the samples demo when both exist."""
        ws = _MockWs(
            catalogs=[_named("samples"), _named("my_catalog"), _named("system")],
            schemas_by_catalog={
                "samples": [_named("nyctaxi")],
                "my_catalog": [_named("information_schema"), _named("sales")],
            },
            warehouses=[_wh("wh_1", "RUNNING")],
        )
        out = _pick_workspace_defaults(ws)
        assert out["DEMO_CATALOG"] == "my_catalog"
        assert out["DEMO_SCHEMA"] == "sales"  # skipped information_schema

    def test_falls_back_to_samples_when_no_user_catalogs(self):
        """A brand-new workspace with only samples still pre-fills usefully."""
        ws = _MockWs(
            catalogs=[_named("samples"), _named("system")],
            schemas_by_catalog={"samples": [_named("tpch"), _named("nyctaxi")]},
            warehouses=[_wh("wh_1", "STOPPED")],
        )
        out = _pick_workspace_defaults(ws)
        assert out["DEMO_CATALOG"] == "samples"
        assert out["DEMO_SCHEMA"] == "nyctaxi"  # explicit nyctaxi preference inside samples

    def test_skips_system_catalogs(self):
        """``system`` and ``__databricks_internal`` must never be picked."""
        ws = _MockWs(
            catalogs=[_named("system"), _named("__databricks_internal"), _named("user_cat")],
            schemas_by_catalog={"user_cat": [_named("default")]},
            warehouses=[_wh("wh_1", "RUNNING")],
        )
        out = _pick_workspace_defaults(ws)
        assert out["DEMO_CATALOG"] == "user_cat"

    def test_warehouse_prefers_running_over_stopped(self):
        """RUNNING avoids a 20-30s cold-start on the first query."""
        ws = _MockWs(
            catalogs=[],
            schemas_by_catalog={},
            warehouses=[
                _wh("stopped_1", "STOPPED", serverless=True),
                _wh("running_1", "RUNNING", serverless=False),
            ],
        )
        out = _pick_workspace_defaults(ws)
        assert out["WAREHOUSE_ID"] == "running_1"

    def test_warehouse_prefers_serverless_when_none_running(self):
        """Serverless beats classic when nothing is already running."""
        ws = _MockWs(
            catalogs=[],
            schemas_by_catalog={},
            warehouses=[
                _wh("classic_1", "STOPPED", serverless=False),
                _wh("serverless_1", "STOPPED", serverless=True),
            ],
        )
        out = _pick_workspace_defaults(ws)
        assert out["WAREHOUSE_ID"] == "serverless_1"

    def test_returns_empty_when_workspace_unreachable(self):
        """SDK errors must not crash the Setup page — fall back to empty."""
        def _boom():
            raise RuntimeError("boom")

        broken = SimpleNamespace(
            catalogs=SimpleNamespace(list=_boom),
            warehouses=SimpleNamespace(list=_boom),
        )
        out = _pick_workspace_defaults(broken)
        assert out == {}


class TestLandingRender:
    def _ctx(self, *, tools, examples):
        from apx_agent._models import AgentTool

        cfg = AgentConfig(name="demo-agent", description="A demo agent.", examples=examples)
        tool_objs = [
            AgentTool(name=n, description=d, input_schema={"type": "object", "properties": {}})
            for n, d in tools
        ]
        card = AgentCard(name="demo-agent", description="A demo agent.", skills=[])
        return AgentContext(config=cfg, tools=tool_objs, card=card, agent=None)  # type: ignore[arg-type]

    def test_landing_shows_greeting_cards_and_chips(self):
        from apx_agent._ui_chat import _render_agent_ui
        html = _render_agent_ui(self._ctx(
            tools=[("sample_customer", "Preview rows."), ("run_sql", "Run SQL.")],
            examples=["Show me sample customers", "Top 5 by balance"],
        ))
        assert 'id="landing"' in html
        assert "demo-agent" in html and "A demo agent." in html
        assert "sample_customer" in html and "run_sql" in html      # capability cards
        assert "Show me sample customers" in html and "Top 5 by balance" in html  # chips

    def test_landing_no_tools_no_chips_still_has_greeting(self):
        # Use _render_landing directly — the full page HTML includes class="starter-chips"
        # in a JS template string even when there are no examples to show.
        from apx_agent._ui_chat import _render_landing
        html = _render_landing(self._ctx(tools=[], examples=[]))
        assert 'id="landing"' in html
        assert "demo-agent" in html
        assert 'class="cap-cards"' not in html       # no capability cards
        assert 'class="starter-chips"' not in html   # no chips

    def test_landing_examples_only_no_tools(self):
        from apx_agent._ui_chat import _render_agent_ui
        html = _render_agent_ui(self._ctx(tools=[], examples=["Hi there"]))
        assert 'class="starter-chips"' in html and "Hi there" in html
        assert 'class="cap-cards"' not in html


class TestTraceDetailSpanEvents:
    def test_render_trace_detail_shows_span_events(self):
        from apx_agent._dev import _render_trace_detail
        spans = [{
            "span_id": "s1", "parent_id": None, "name": "run_sql",
            "span_type": "TOOL", "status": "OK",
            "start_time_ns": 0, "end_time_ns": 1_000_000, "duration_ms": 1.0,
            "inputs": None, "outputs": None,
            "events": [{"name": "apx.progress",
                        "attributes": {"message": "Starting SQL warehouse — serverless cold-start, ~20-30s"}}],
        }]
        html = _render_trace_detail("tr-1", spans, None)
        assert "Starting SQL warehouse" in html

    def test_render_trace_detail_shows_exception_event_details(self):
        from apx_agent._dev import _render_trace_detail

        spans = [{
            "span_id": "s1", "parent_id": None, "name": "run_sql",
            "span_type": "TOOL", "status": "ERROR",
            "start_time_ns": 0, "end_time_ns": 1_000_000, "duration_ms": 1.0,
            "inputs": None, "outputs": None,
            "events": [{"name": "exception", "attributes": {
                "exception.type": "ValueError",
                "exception.message": "warehouse failed",
                "exception.stacktrace": "Traceback (most recent call last):\\nValueError: warehouse failed",
            }}],
        }]

        html = _render_trace_detail("tr-1", spans, None)
        assert "ValueError: warehouse failed" in html
        assert "Traceback (most recent call last)" in html


class TestEventsToolCalls:
    """The chat stream surfaces tool calls (+ their SQL/args) as Events live,
    independent of the trace-span harvest (which needs blob-storage egress).
    Regression for tool calls being invisible in Events on FEVM workspaces."""

    def test_stream_handler_emits_tool_call_events(self):
        from apx_agent._ui_chat import _render_agent_ui
        from apx_agent import AgentConfig, AgentContext
        from apx_agent._models import AgentTool
        cfg = AgentConfig(name="d", description="x", examples=[])
        ctx = AgentContext(
            config=cfg,
            tools=[AgentTool(name="run_sql", description="Run SQL",
                             input_schema={"type": "object", "properties": {}})],
            card={"name": "d", "skills": []}, agent=None,  # type: ignore[arg-type]
        )
        html = _render_agent_ui(ctx)
        # function_call / function_call_output items become tool events from the stream
        assert "item.type === 'function_call'" in html
        assert "addToolCall(" in html
        assert "item.type === 'function_call_output'" in html
        assert "addToolResponse(" in html
        # and finalizeTrace is told not to double-emit when the stream already did
        assert "emitEvents: !toolEventsFromStream" in html

    def test_tool_events_grouped_by_call(self):
        from apx_agent._ui_chat import _render_agent_ui
        from apx_agent import AgentConfig, AgentContext
        from apx_agent._models import AgentTool
        cfg = AgentConfig(name="d", description="x", examples=[])
        ctx = AgentContext(
            config=cfg,
            tools=[AgentTool(name="run_sql", description="Run SQL",
                             input_schema={"type": "object", "properties": {}})],
            card={"name": "d", "skills": []}, agent=None,  # type: ignore[arg-type]
        )
        html = _render_agent_ui(ctx)
        # A tool call + its response are grouped into one block keyed by call_id.
        assert "function addToolCall" in html
        assert "function addToolResponse" in html
        assert "toolGroups" in html                 # the call_id -> group map
        assert "tool-group" in html                 # the group container styling
        # The grouped block labels the two parts request / response.
        assert ">request<" in html
        assert "'error' : 'response'" in html  # the response/error label ternary


class TestInlineSteps:
    """The chat stream also renders each tool call as an expandable step row
    in the transcript, above the streaming answer bubble (Genie-Code style).
    Additive to the Events panel (TestEventsToolCalls) — both fed from the
    same function_call / function_call_output stream items, keyed by call_id."""

    def test_stream_renders_inline_tool_steps(self):
        from apx_agent._ui_chat import _render_agent_ui
        from apx_agent import AgentConfig, AgentContext
        from apx_agent._models import AgentTool
        cfg = AgentConfig(name="d", description="x", examples=[])
        ctx = AgentContext(
            config=cfg,
            tools=[AgentTool(name="run_sql", description="Run SQL",
                             input_schema={"type": "object", "properties": {}})],
            card={"name": "d", "skills": []}, agent=None,  # type: ignore[arg-type]
        )
        html = _render_agent_ui(ctx)
        assert "function renderInlineStep" in html          # the renderer exists
        assert "stepsContainer" in html                      # transcript container
        assert "renderInlineStep(" in html                   # called from the stream branches
        assert "insertBefore(stepsContainer" in html         # steps sit above the answer bubble
        assert ".inline-step" in html                        # styling present

    def test_inline_step_keeps_both_request_and_response(self):
        """The step must show the tool's REQUEST (args/SQL) AND its RESPONSE
        (rows) — not overwrite the query with the result. For a SQL tool the
        query lives in the call args, so dropping the request hides the SQL."""
        from apx_agent._ui_chat import _render_agent_ui
        from apx_agent import AgentConfig, AgentContext
        from apx_agent._models import AgentTool
        cfg = AgentConfig(name="d", description="x", examples=[])
        ctx = AgentContext(
            config=cfg,
            tools=[AgentTool(name="run_sql", description="Run SQL",
                             input_schema={"type": "object", "properties": {}})],
            card={"name": "d", "skills": []}, agent=None,  # type: ignore[arg-type]
        )
        html = _render_agent_ui(ctx)
        # Renderer tracks request + response separately (not a single `detail`).
        assert "request:" in html      # function_call branch passes the args as request
        assert "response:" in html     # function_call_output branch passes the output
        assert "Request" in html       # labeled section in the expanded detail
        assert "Response" in html      # labeled section in the expanded detail


class TestTraceDeltaRender:
    """The trace detail renders chat payloads as a compact, role-labeled
    conversation and elides messages already shown one level up — so nesting
    no longer re-prints the system prompt + prior turns at every level."""

    def test_common_prefix_len(self):
        from apx_agent._dev import _common_prefix_len
        a = [{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}]
        b = a + [{"role": "assistant", "content": "yo"}]
        assert _common_prefix_len(a, b) == 2
        assert _common_prefix_len(a, []) == 0
        assert _common_prefix_len([], b) == 0

    def test_is_chat_messages_detects_payload(self):
        from apx_agent._dev import _is_chat_messages
        assert _is_chat_messages({"messages": [{"role": "user", "content": "hi"}]}) is not None
        assert _is_chat_messages({"statement": "SELECT 1"}) is None
        assert _is_chat_messages("not a dict") is None
        assert _is_chat_messages({"messages": []}) is None

    def test_messages_block_collapses_shared_prefix(self):
        from apx_agent._dev import _render_messages_block
        prev = [
            {"role": "system", "content": "S" * 200},
            {"role": "user", "content": "how many customers"},
        ]
        msgs = prev + [
            {"role": "assistant", "content": None,
             "tool_calls": [{"function": {"name": "run_sql",
                                          "arguments": '{"query": "SELECT 1"}'}}]},
            {"role": "tool", "content": '{"row_count": 1}'},
        ]
        html = _render_messages_block(msgs, prev)
        # The shared [system, user] prefix collapses to one expandable line ...
        assert "2 earlier messages" in html
        assert 'class="tprefix"' in html
        # ... and only the NEW messages render expanded.
        assert "run_sql" in html
        assert "row_count" in html
        # The big system prompt only appears INSIDE the collapsed prefix block,
        # never in the new-messages tail rendered after it.
        tail = html.split("</details>", 1)[1] if "</details>" in html else html
        assert "S" * 200 not in tail

    def test_system_prompt_folds_to_one_line(self):
        from apx_agent._dev import _render_messages_block
        msgs = [{"role": "system", "content": "You are a data assistant. " * 30}]
        html = _render_messages_block(msgs, None)
        # Long system prompt is rendered behind a <details> disclosure, not raw.
        assert "<details" in html
        assert "chars)" in html  # the "(NNN chars)" length hint

    def test_conversation_rendered_only_on_llm_spans(self):
        """The conversation lives uniformly on LLM spans. Wrapper spans
        (LangGraph/model CHAIN) re-log the same growing message list in a
        different shape — that re-log IS the down-a-level repeat — so the
        detail must render the compact conversation on LLM spans and suppress
        the message re-dump on non-LLM wrapper spans."""
        from apx_agent._dev import _render_trace_detail
        # An LLM span (system prompt lives here) nested under a CHAIN wrapper
        # that re-logs the same messages in LangChain `type` shape.
        msgs_openai = [
            {"role": "system", "content": "You are a data assistant. " * 20},
            {"role": "user", "content": "how many customers"},
        ]
        msgs_langchain = [
            {"type": "human", "content": "how many customers",
             "additional_kwargs": {}, "response_metadata": {}},
        ]
        spans = [
            {"span_id": "w", "parent_id": None, "name": "model",
             "span_type": "CHAIN", "status": "OK", "duration_ms": 10,
             "inputs": {"messages": msgs_langchain}, "outputs": None, "events": []},
            {"span_id": "l", "parent_id": "w", "name": "ChatDatabricks",
             "span_type": "CHAT_MODEL", "status": "OK", "duration_ms": 8,
             "inputs": {"messages": msgs_openai}, "outputs": None, "events": []},
        ]
        html = _render_trace_detail("tr-x", spans, None)
        # The conversation renders exactly ONCE — on the LLM span — incl. the
        # folded system prompt. The wrapper CHAIN span suppresses its message
        # re-log entirely (that re-log is the down-a-level repeat).
        assert html.count('class="convo"') == 1
        assert "tsys" in html
        # The wrapper's LangChain re-log appears nowhere — not compact, not raw.
        assert "additional_kwargs" not in html  # would appear if raw-dumped
        assert '"type": "human"' not in html
        assert html.count("how many customers") == 1  # shown once, on the LLM span


class TestWarmupTraceFilter:
    """The startup ``apx.trace_capture.warmup`` self-test trace must not show in
    the dev-UI traces list — it carries one internal span and reads as an empty
    trace when opened, which is confusing noise."""

    def test_warmup_traces_dropped(self):
        from apx_agent._dev import _drop_warmup_traces
        from apx_agent._trace_store import WARMUP_SPAN_NAME
        from types import SimpleNamespace

        def mk(name, tid):
            return SimpleNamespace(
                info=SimpleNamespace(trace_id=tid, tags={"mlflow.traceName": name})
            )

        traces = [
            mk("streaming", "tr-1"),
            mk(WARMUP_SPAN_NAME, "tr-2"),
            mk("streaming", "tr-3"),
            mk(WARMUP_SPAN_NAME, "tr-4"),
        ]
        kept = _drop_warmup_traces(traces)
        assert [t.info.trace_id for t in kept] == ["tr-1", "tr-3"]

    def test_drop_warmup_tolerates_missing_tags(self):
        from apx_agent._dev import _drop_warmup_traces
        from types import SimpleNamespace
        t = SimpleNamespace(info=SimpleNamespace(trace_id="tr-x", tags=None))
        assert _drop_warmup_traces([t]) == [t]  # no tags → kept, no crash


class TestSerializeTraceSpans:
    """The shared span->dict serializer used by both the route and the
    in-process trace buffer must produce identical span dicts (incl. events)."""

    def test_serialize_trace_spans_shape(self):
        from apx_agent._dev import _serialize_trace_spans
        from types import SimpleNamespace
        span = SimpleNamespace(
            span_id="s1", parent_id=None, name="run_sql",
            span_type=SimpleNamespace(value="TOOL"),
            status=SimpleNamespace(status_code=SimpleNamespace(value="OK")),
            start_time_ns=0, end_time_ns=1_000_000, inputs={"q": "x"}, outputs=None,
            events=[SimpleNamespace(name="apx.progress", attributes={"message": "hi"})],
        )
        trace = SimpleNamespace(data=SimpleNamespace(spans=[span]))
        out = _serialize_trace_spans(trace)
        assert out[0]["name"] == "run_sql"
        assert out[0]["span_type"] == "TOOL"
        assert out[0]["events"][0]["attributes"]["message"] == "hi"


class TestTraceDetailBufferAndFailFast:
    """The trace-detail route serves recent traces from the in-process ring
    buffer first, and on a buffer miss falls through to a TIME-BOUNDED
    mlflow.get_trace that fails fast (instead of hanging on blocked blob
    egress)."""

    @pytest.mark.asyncio
    async def test_buffer_hit_serves_without_calling_get_trace(self, app: FastAPI):
        from unittest.mock import patch
        from apx_agent import _trace_store as ts
        ts.reset()
        ts.put("tr-hit", [{"name": "get_time", "span_type": "TOOL", "events": []}])

        def _boom(*a, **k):  # get_trace must NOT be called on a buffer hit
            raise AssertionError("mlflow.get_trace called on a buffer hit")

        with patch("mlflow.get_trace", side_effect=_boom):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
                r = await ac.get("/_apx/traces/tr-hit?fmt=json")
        assert r.status_code == 200
        body = r.json()
        assert body["trace_id"] == "tr-hit"
        assert body["spans"][0]["name"] == "get_time"

    @pytest.mark.asyncio
    async def test_buffer_miss_slow_get_trace_fails_fast(self, app: FastAPI):
        import time
        from unittest.mock import patch
        from apx_agent import _trace_store as ts
        ts.reset()

        def _slow(*a, **k):
            time.sleep(30)  # simulate the blocked-blob hang
            return None

        start = time.monotonic()
        with patch("mlflow.get_trace", side_effect=_slow):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
                r = await ac.get("/_apx/traces/tr-miss?fmt=json")
        elapsed = time.monotonic() - start
        assert elapsed < 8, f"route hung for {elapsed:.1f}s — fail-fast timeout did not fire"
        assert r.status_code == 200
        assert "unavailable" in r.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_buffer_miss_ok_get_trace_serves_and_populates_buffer(self, app: FastAPI):
        from types import SimpleNamespace
        from unittest.mock import patch
        from apx_agent import _trace_store as ts
        ts.reset()
        span = SimpleNamespace(
            span_id="s1", parent_id=None, name="get_time",
            span_type=SimpleNamespace(value="TOOL"),
            status=SimpleNamespace(status_code=SimpleNamespace(value="OK")),
            start_time_ns=0, end_time_ns=1_000_000, inputs={}, outputs=None, events=[],
        )
        trace = SimpleNamespace(data=SimpleNamespace(spans=[span]))
        with patch("mlflow.get_trace", return_value=trace):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
                r = await ac.get("/_apx/traces/tr-ok?fmt=json")
        assert r.status_code == 200
        assert r.json()["spans"][0]["name"] == "get_time"
        # The successful fetch populates the buffer for next time.
        assert ts.get("tr-ok") is not None
        assert ts.get("tr-ok")[0]["name"] == "get_time"


class TestAgentContextSchema:
    def test_context_accepts_schema(self):
        from apx_agent import AgentConfig, AgentContext
        cfg = AgentConfig(name="d", description="x", examples=[])
        ctx = AgentContext(
            config=cfg, tools=[], card={"name": "d", "skills": []},
            agent=None,  # type: ignore[arg-type]
            schema={"catalog": "samples", "schema": "tpch", "tables": {"t": ["a(int)"]}},
        )
        assert ctx.schema["tables"] == {"t": ["a(int)"]}

    def test_schema_defaults_none(self):
        from apx_agent import AgentConfig, AgentContext
        cfg = AgentConfig(name="d", description="x", examples=[])
        ctx = AgentContext(config=cfg, tools=[], card={"name": "d", "skills": []},
                           agent=None)  # type: ignore[arg-type]
        assert ctx.schema is None


class TestLandingDataCard:
    def _ctx(self, schema):
        from apx_agent import AgentConfig, AgentContext
        cfg = AgentConfig(name="d", description="x", examples=[])
        return AgentContext(config=cfg, tools=[], card={"name": "d", "skills": []},
                            agent=None, schema=schema)  # type: ignore[arg-type]

    def test_card_renders_tables_and_columns(self):
        from apx_agent._ui_chat import _render_landing
        ctx = self._ctx({"catalog": "samples", "schema": "tpch",
                         "tables": {"customer": ["c_custkey(bigint)", "c_name(string)"]}})
        html = _render_landing(ctx)
        assert "tpch" in html       # card header shows schema name (not catalog.schema)
        assert "customer" in html   # table name rendered as a pill
        assert "data-card" in html  # the card container styling/class
        # column names are not rendered in the landing card (table pill view only)

    def test_card_omitted_without_schema(self):
        from apx_agent._ui_chat import _render_landing
        html = _render_landing(self._ctx(None))
        assert "data-card" not in html


class TestEditReadOnlyConfigAgent:
    """The Edit page renders a read-only Python view for config-only agents
    instead of the empty 'agent source not found' page."""

    def test_render_edit_ui_read_only_shows_code_and_live_tools(self):
        from apx_agent._ui_edit import _render_edit_ui
        code = 'from apx_agent.coworker import CoworkerAgent\n\nagent = CoworkerAgent("c", "s")\n'
        html = _render_edit_ui(
            code,
            read_only=True,
            initial_schemas=[{"name": "run_sql", "description": "runs sql", "parameters": {}}],
        )
        assert "CoworkerAgent" in html          # the synthesized code is shown
        assert "const READ_ONLY = true" in html  # editor is read-only
        assert "read-only" in html               # informative banner, not "not found"
        assert "run_sql" in html                 # live tool schema injected
        assert "agent source not found" not in html

    def test_render_edit_ui_default_is_editable(self):
        from apx_agent._ui_edit import _render_edit_ui
        html = _render_edit_ui("agent = 1\n")
        assert "const READ_ONLY = false" in html


def test_trace_detail_hides_new_token_events_keeps_exceptions():
    """Streaming logs one 'new_token' event per token — hundreds per span. The
    full-trace view must skip them (they bury the real spans) while still
    surfacing meaningful events like exceptions."""
    from apx_agent._dev import _render_trace_detail

    spans = [{
        "span_id": "a", "parent_id": None, "name": "knowledge_assistant",
        "span_type": "CHAIN", "status": "OK", "duration_ms": 5,
        "inputs": None, "outputs": None,
        "events": [{"name": "new_token", "attributes": {}} for _ in range(50)]
        + [{"name": "exception",
            "attributes": {"exception.type": "ValueError", "exception.message": "boom"}}],
    }]
    html = _render_trace_detail("tr-x", spans, None)
    assert "▸ new_token" not in html, "new_token events must be hidden"
    assert "boom" in html, "real exception event must still render"
