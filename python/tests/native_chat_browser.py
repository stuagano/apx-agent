"""Run explicitly: uv run --frozen --with playwright pytest tests/native_chat_browser.py.

Real Chromium DOM/network assertions against rendered pages and the frozen SDK wire
contract. The separate SDK contract suite covers the real runtime, not these routes.
"""

import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
# Optional: CI runs a PR's changed test files and has no Playwright/Chromium.
playwright_sync = pytest.importorskip("playwright.sync_api")
sync_playwright, expect = playwright_sync.sync_playwright, playwright_sync.expect

from apx_agent._ui_chat import _render_agent_ui
from apx_agent._ui_root_chat import render_root_chat
from tests.test_native_invocation_contract import native, headers  # noqa: F401
from tests.test_runtime_targets import execution  # noqa: F401
from tests.test_dev_ui_routes import _make_ctx
from pydantic import BaseModel, ConfigDict, ValidationError
from apx_agent._native_input import native_data_part


class BrowserInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: int
    as_of: str


class NumericInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: int | float


@pytest.fixture(params=["root", "dev"])
def browser_chat(request):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        surface = request.param
        html = render_root_chat(native=True) if surface == "root" else _render_agent_ui(_make_ctx(), native=True)
        posts, reads = [], []
        state = {"status": "completed", "output": {"status": "completed", "messages": [
            {"type": "ai", "content": "Recorded proof"}]}}
        event = {"type": "agent.message.completed", "message": {"type": "tool", "content": "tool proof"}}

        def route(req):
            url = req.request.url
            if url.endswith("/api/input-schema"):
                req.fulfill(content_type="application/json", body=json.dumps(state.get("input_schema")))
            elif url.endswith("/api/input-validation"):
                try:
                    BrowserInput.model_validate_json(req.request.post_data, strict=True)
                    req.fulfill(json=native_data_part(BrowserInput, req.request.post_data_json))
                except (ValidationError, ValueError):
                    req.fulfill(status=422, json={"detail": "Check required amount (integer) and as_of (string)"})
            elif "/api/invocations" in url:
                if req.request.method == "POST":
                    posts.append(req.request.post_data_json)
                    req.fulfill(status=202, json={"id": posts[-1]["id"], "status": "active"})
                elif "/events" in url:
                    reads.append(url)
                    req.fulfill(content_type="text/event-stream", body=f"id: 1\nevent: agent.message.completed\ndata: {json.dumps(event)}\n\n")
                else:
                    reads.append(url)
                    req.fulfill(json={"id": posts[-1]["id"], **state})
            elif url.endswith("/"):
                req.fulfill(content_type="text/html", body=html)
            elif "/vendor/" in url:
                vendor = Path(__file__).parents[1] / "src/apx_agent/_static/vendor" / url.rsplit("/", 1)[-1]
                req.fulfill(content_type="application/javascript", body=vendor.read_text())
            else:
                req.fulfill(status=404, json={"detail": "Not found"})

        page.route("**/*", route)
        page.goto("http://localhost/")
        yield page, posts, reads, state, event
        assert errors == []
        browser.close()


def submit(page, text="record"):
    page.locator("form textarea").fill(text)
    page.locator("button[type=submit]").click()


def test_native_question_and_stable_session(browser_chat):
    page, posts, reads, state, event = browser_chat
    submit(page)
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    assert len(posts) == 1
    body = posts[0]
    uuid.UUID(body["id"])
    uuid.UUID(body["session_id"])
    assert body == {"id": body["id"], "session_id": body["session_id"],
                    "input": {"messages": [{"role": "user", "content": "record"}]},
                    "background": True, "stream": True}
    submit(page, "next")
    expect(page.locator(".msg.assistant")).to_have_count(2)
    expect(page.locator(".msg.assistant").last).to_have_text("Recorded proof")
    assert posts[1]["id"] != body["id"]
    assert posts[1]["session_id"] == body["session_id"]
    assert len(reads) >= 4


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_native_terminal_states(browser_chat, status):
    page, posts, reads, state, event = browser_chat
    if status == "failed":
        state.update(status="failed", error="sanitized failure")
    else:
        state["output"].update(status="interrupted", approval_required={"tool_name": "record"})
    submit(page)
    expect(page.locator(".native-status")).to_contain_text(status.capitalize())
    expect(page.locator(".msg.assistant")).to_have_count(0)
    assert len(posts) == 1


def test_untrusted_result_and_event_text(browser_chat):
    page, posts, reads, state, event = browser_chat
    attack = '<img src=x onerror="window.injected=1">'
    event["message"]["content"] = attack
    state["output"]["messages"][0]["content"] = attack
    submit(page)
    expect(page.locator(".native-status")).to_have_text("Completed")
    assert page.evaluate("window.injected") is None
    assert page.locator("[onerror]").count() == 0
    assert json.loads(page.locator(".native-event").text_content())["message"]["content"] == attack


def test_reload_replays_suffix_without_resubmitting(browser_chat):
    page, posts, reads, state, event = browser_chat
    state["status"] = "active"
    submit(page)
    expect(page.locator(".native-status")).to_contain_text("Reconnect")
    assert len(posts) == 1
    state["status"] = "completed"
    suffix = {"type": "agent.message.completed", "message": {"type": "tool", "content": "second proof"}}

    def replay(route):
        reads.append(route.request.url)
        # Include a duplicate defensively: the persisted exclusive cursor wins.
        route.fulfill(content_type="text/event-stream", body=
                      f"id: 1\ndata: {json.dumps(event)}\n\nid: 2\ndata: {json.dumps(suffix)}\n\n")

    page.route("**/events?after=1", replay)
    page.reload()
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    expect(page.locator(".native-event")).to_have_count(2)
    expect(page.locator(".native-event").last).to_contain_text("second proof")
    assert any(url.endswith("/events?after=1") for url in reads)
    assert len(posts) == 1


def test_ambiguous_post_keeps_exact_retry(browser_chat):
    page, posts, reads, state, event = browser_chat
    def lost(route):
        posts.append(route.request.post_data_json)
        route.abort("failed")
    page.route("**/api/invocations", lost)
    submit(page)
    expect(page.locator(".native-status")).to_contain_text("Reconnect")
    assert len(posts) == 1
    page.unroute("**/api/invocations", lost)
    page.get_by_role("button", name="Retry same request", exact=True).click()
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    assert len(posts) == 1
    assert any('/api/invocations/' + posts[0]['id'] in url for url in reads)


def test_ambiguous_post_retries_exact_request_only_after_same_id_is_absent(browser_chat):
    page, posts, reads, state, event = browser_chat
    def lost(route):
        posts.append(route.request.post_data_json)
        route.abort("failed")
    page.route("**/api/invocations", lost)
    submit(page)
    expect(page.locator(".native-status")).to_contain_text("Connection uncertain")
    original = posts[0]
    page.unroute("**/api/invocations", lost)
    missing_reads = []
    def absent_once(route):
        missing_reads.append(route.request.url)
        if len(missing_reads) == 1:
            route.fulfill(status=404, json={"detail": "Not found"})
        else:
            route.fallback()
    page.route("**/api/invocations/*", absent_once)
    page.get_by_role("button", name="Retry same request", exact=True).click()
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    assert missing_reads[0].endswith('/api/invocations/' + original['id'])
    assert posts == [original, original]


def test_native_message_content_blocks(browser_chat):
    page, posts, reads, state, event = browser_chat
    state["output"]["messages"] = [
        {"type": "tool", "content": "not an assistant answer"},
        {"type": "ai", "content": [{"type": "text", "text": "Block answer"}]},
    ]
    submit(page)
    expect(page.locator(".msg.assistant")).to_have_text("Block answer")


def test_native_halt_message_is_the_whole_answer(browser_chat):
    # A halted sequence's follow-up question replaces the step's preamble and recorded JSON.
    page, posts, reads, state, event = browser_chat
    state["output"]["messages"] = [
        {"type": "ai", "content": "I'll draft the request."},
        {"type": "ai", "content": '{"status": "needs_input"}', "additional_kwargs": {"apx_data_parts": [
            {"kind": "data", "data": {"status": "needs_input"}}]}},
        {"type": "ai", "content": "Which bank?", "additional_kwargs": {"apx_answer": True}},
    ]
    submit(page)
    answer = page.locator(".msg.assistant")
    expect(answer).to_contain_text("Which bank?")
    shown = answer.evaluate("e => { const c = e.cloneNode(true); c.querySelector('.native-recorded-output')?.remove(); return c.textContent.trim(); }")
    assert shown == "Which bank?"  # the exact JSON stays only in the recorded-output details


def test_native_halt_message_in_content_blocks(browser_chat):
    page, posts, reads, state, event = browser_chat
    state["output"]["messages"] = [
        {"type": "ai", "content": "preamble"},
        {"type": "ai", "content": [{"type": "text", "text": "Which bank?"}], "additional_kwargs": {"apx_answer": True}},
    ]
    submit(page)
    expect(page.locator(".msg.assistant")).to_have_text("Which bank?")


def test_native_typed_submission_requires_complete_declared_input(browser_chat):
    page, posts, reads, state, event = browser_chat
    state["input_schema"] = BrowserInput.model_json_schema()
    page.reload()
    expect(page.locator(".native-input-schema")).to_contain_text("as_of")
    submit(page, '{"amount":4}')
    expect(page.locator(".native-validation")).to_contain_text("as_of")
    assert posts == []
    payload = {"amount": 4, "as_of": "2026-10-22"}
    submit(page, json.dumps(payload))
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    assert posts[0]["input"] == {"messages": [], "apx_data_parts": [native_data_part(BrowserInput, payload)]}
    page.reload()
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    assert len(posts) == 1


@pytest.mark.parametrize("number", ["1.0", "9007199254740993"])
def test_native_typed_numeric_wire_survives_preflight_post_and_saved_retry(browser_chat, number):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from apx_agent import LlmAgent
    from apx_agent._ui_native_chat import native_input_router

    page, posts, reads, state, event = browser_chat
    app = FastAPI()
    app.include_router(native_input_router(LlmAgent(input_schema=NumericInput, input_key="request")))
    state["input_schema"] = NumericInput.model_json_schema()
    state["status"] = "active"
    payload_wire = ' {\n"amount":' + number + '\n} '
    validation_wires, request_wires, missing_reads = [], [], []

    with TestClient(app) as client:
        def preflight(route):
            validation_wires.append(route.request.post_data)
            response = client.post("/api/input-validation", content=route.request.post_data,
                                   headers={"Content-Type": "application/json"})
            route.fulfill(status=response.status_code, content_type="application/json", body=response.content)

        def invoke(route):
            wire = route.request.post_data
            request_wires.append(wire)
            body = json.loads(wire)
            posts.append(body)
            part = body["input"]["apx_data_parts"][0]
            # The actual ingress integrity helper must accept the browser's exact wire.
            assert part == native_data_part(NumericInput, part["data"], part)
            assert part["data"] == json.loads(payload_wire)
            assert type(part["data"]["amount"]) is type(json.loads(payload_wire)["amount"])
            if len(request_wires) == 1:
                route.abort("failed")
            else:
                state["status"] = "completed"
                route.fulfill(status=202, json={"id": body["id"], "status": "active"})

        def absent_once(route):
            missing_reads.append(route.request.url)
            if len(missing_reads) == 1:
                route.fulfill(status=404, json={"detail": "Not found"})
            else:
                route.fallback()

        page.route("**/api/input-validation", preflight)
        page.route("**/api/invocations", invoke)
        page.reload()
        submit(page, payload_wire)
        expect(page.locator(".native-status")).to_contain_text("Connection uncertain")
        assert validation_wires == [payload_wire]
        cached = json.loads(page.evaluate("sessionStorage.getItem('_apx_native_chat:/')"))
        assert cached["requestWire"] == request_wires[0]
        page.reload()
        expect(page.locator(".native-status")).to_contain_text("Reconnect")
        expect(page.locator(".msg.user")).to_have_text(payload_wire)
        assert len(request_wires) == 1
        page.route("**/api/invocations/*", absent_once)
        page.get_by_role("button", name="Retry same request", exact=True).click()
        expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
        assert request_wires == [cached["requestWire"], cached["requestWire"]]
        page.reload()
        expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
        expect(page.locator(".msg.user")).to_have_text(payload_wire)
        assert len(request_wires) == 2


@pytest.mark.parametrize("number", ["1.0", "9007199254740993"])
def test_native_recorded_output_preserves_numeric_wire(browser_chat, number):
    page, posts, reads, state, event = browser_chat
    part = native_data_part(NumericInput, json.loads('{"amount":' + number + '}'))
    state["output"]["messages"][0]["additional_kwargs"] = {"apx_data_parts": [part]}
    event["message"]["additional_kwargs"] = {"apx_data_parts": [part]}
    response_wire = json.dumps(state)
    def recorded(route):
        if "/events" in route.request.url:
            route.fallback()
        else:
            route.fulfill(content_type="application/json", body=response_wire)
    page.route("**/api/invocations/*", recorded)
    submit(page)
    expect(page.locator(".native-output")).to_have_text(response_wire)
    expect(page.locator(".native-event")).to_have_text(json.dumps(event))
    expect(page.locator(".native-recorded-output summary")).to_have_text("Recorded invocation response (exact JSON)")
    assert json.loads(page.locator(".native-output").text_content())["output"]["messages"][0]["additional_kwargs"]["apx_data_parts"] == [part]
    page.reload()
    expect(page.locator(".native-output")).to_have_text(response_wire)
    expect(page.locator(".native-event")).to_have_text(json.dumps(event))
    assert len(posts) == 1


def test_native_execution_card_separates_status_and_provenance(browser_chat):
    page, posts, reads, state, event = browser_chat
    event.clear()
    event.update(type="agent.step.completed", step="calibrate", location="remote", invocation_id="parent-id",
                 child_invocation_id="child-id", parent_invocation_id="parent-id", transport_sender="fleet",
                 child_invocation_url="https://calibrate.example/api/invocations/child-id",
                 logical_predecessor="intelligence", transport_status="completed", domain_status="not_ready",
                 schema_validated=True, input_schema="CalibrateInput", output_schema="PricingDecision",
                 input_artifact={"artifact_id": "input-actual", "sha256": "a" * 64},
                 output_artifact={"artifact_id": "output-actual", "sha256": "b" * 64},
                 recorded_at="2026-10-22T18:00:00+00:00", timestamp_source="application_emit", readback_verified=True)
    part = {"kind": "data", "data": {"decision": {"status": "not_ready"}, "proof": '<img src=x onerror="window.injected=1">'},
            "metadata": {"artifact_id": "output-actual", "sha256": "b" * 64}}
    state["output"]["messages"][0]["additional_kwargs"] = {"apx_data_parts": [part]}
    submit(page)
    card = page.locator(".native-execution-card")
    expect(card).to_have_count(1)
    record = card.locator("details.native-step-record")
    for text in ["sender fleet", "predecessor intelligence", "input CalibrateInput aaaaaaaa…", "output PricingDecision bbbbbbbb…",
                 "2026-10-22 18:00:00 UTC", "schema validated ✓", "readback verified ✓",
                 "not recorded: child timing, trace, model/version"]:
        expect(record).to_contain_text(text)
    expect(record).not_to_contain_text("unavailable")  # missing facts are one line, not a row each
    assert record.locator("[title='" + "b" * 64 + "']").count() == 1  # the full hash stays one hover away
    card.locator("details > summary", has_text="Details").click()  # provenance is one click away
    assert card.get_by_role("link", name="child-id", exact=True).get_attribute("href") == "https://calibrate.example/api/invocations/child-id"
    assert json.loads(page.locator(".native-output").text_content())["output"]["messages"][0]["additional_kwargs"]["apx_data_parts"] == [part]
    assert page.evaluate("window.injected") is None
    assert page.locator("[onerror]").count() == 0
    page.reload()
    expect(page.locator(".native-execution-card")).to_have_count(1)
    assert len(posts) == 1


def test_native_execution_card_is_one_readable_line(browser_chat):
    # A person reads step, outcome and location; the provenance stays one click away.
    page, posts, reads, state, event = browser_chat
    event.clear()
    event.update(type="agent.step.completed", step="calibrate", location="remote", invocation_id="parent-id",
                 transport_status="completed", domain_status="not_ready", schema_validated=True,
                 output_artifact={"artifact_id": "output-actual", "sha256": "b" * 64})
    submit(page)
    card = page.locator(".native-execution-card")
    expect(card.locator(".native-step-summary")).to_have_text("✓ calibrate · not ready · remote")
    expect(card.locator("details.native-step-record")).not_to_have_attribute("open", "")
    expect(card.get_by_text("schema validated ✓")).to_be_hidden()
    expect(card).to_contain_text("output bbbbbbbb…")  # still in the record


def test_native_step_line_omits_local_and_names_the_remote_app(browser_chat):
    page, posts, reads, state, event = browser_chat
    event.clear()
    event.update(type="agent.step.completed", step="quote", location="local", invocation_id="parent-id",
                 transport_status="completed", domain_status="ready")
    submit(page)
    expect(page.locator(".native-step-summary")).to_have_text("✓ quote · ready")
    event.clear()
    event.update(type="agent.step.completed", step="price", location="remote", invocation_id="parent-id",
                 transport_status="completed", domain_status="recommended", child_invocation_id="child-id",
                 child_invocation_url="https://pricing-app-prod-1234567890.aws.databricksapps.com/api/invocations/child-id")
    submit(page)
    expect(page.locator(".native-step-summary").last).to_have_text("✓ price · recommended · → pricing-app-prod")
    host = "pricing-app-prod-1234567890.aws.databricksapps.com"
    # The short label is derived from the event's URL, so the full host is always shown with it.
    expect(page.locator(".native-step-summary").last).to_have_attribute("title", "ran in " + host)
    expect(page.locator("details.native-step-record").last).to_contain_text("ran in " + host)


def test_native_record_table_ignores_inherited_keys(browser_chat):
    page, posts, reads, state, event = browser_chat
    _typed_step(event, state, {"rows": [{"constructor": 1, "a": 2}, {"a": 3}]})
    submit(page)
    view = page.locator("details.native-step-output")
    view.locator(":scope > summary").click()
    view.locator("summary", has_text="rows").click()
    expect(view.locator("table.native-tree-table tbody tr").nth(1)).not_to_contain_text("function")


def _typed_step(event, state, data, sha="c" * 64):
    event.clear()
    event.update(type="agent.step.completed", step="quote", location="remote", invocation_id="parent-id",
                 transport_status="completed", domain_status="recommended", output_schema="QuoteV1",
                 output_artifact={"artifact_id": "a1", "sha256": sha})
    state["output"]["messages"] = [
        {"type": "ai", "content": "Done.", "additional_kwargs": {"apx_data_parts": [
            {"kind": "data", "data": data, "metadata": {"artifact_id": "a1", "sha256": sha}}]}}]


def test_native_step_card_shows_its_typed_output_as_structure(browser_chat):
    # The step's exact typed artifact (matched by sha) renders as a collapsed key/value tree.
    page, posts, reads, state, event = browser_chat
    _typed_step(event, state, {"selected": 5, "options": [{"bps": 0}, {"bps": 5}],
                               "decision": {"status": "recommended"}, "note": "<b>x</b>"})
    submit(page)
    view = page.locator(".native-execution-card details.native-step-output")
    expect(view).to_have_count(1)
    expect(view.locator(":scope > summary")).to_have_text("Output · QuoteV1")
    expect(view).not_to_have_attribute("open", "")
    view.locator(":scope > summary").click()
    top = view.locator(":scope > .native-tree")
    # One level at a time: plain values show, nested fields are closed rows with a count.
    expect(top).to_contain_text("selected 5")
    expect(top).to_contain_text("note <b>x</b>")
    expect(top.locator(":scope > details > summary")).to_have_text(["options 2 items", "decision 1 field"])
    expect(top.get_by_text("recommended")).to_be_hidden()
    top.locator("summary", has_text="decision").click()
    expect(top.get_by_text("recommended")).to_be_visible()
    assert view.locator(".native-tree b").count() == 0  # values are text, never markup


def test_native_list_of_flat_records_opens_as_a_table(browser_chat):
    page, posts, reads, state, event = browser_chat
    _typed_step(event, state, {"scenarios": [{"bps": 0, "rate": 480}, {"bps": 5, "rate": 485}]})
    submit(page)
    view = page.locator("details.native-step-output")
    view.locator(":scope > summary").click()
    view.locator("summary", has_text="scenarios").click()
    table = view.locator("table.native-tree-table")
    expect(table.locator("th")).to_have_text(["bps", "rate"])
    expect(table.locator("tbody tr")).to_have_count(2)
    expect(table.locator("tbody tr").nth(1)).to_have_text("5485")


def test_native_step_output_only_matches_its_own_artifact(browser_chat):
    page, posts, reads, state, event = browser_chat
    _typed_step(event, state, {"selected": 5}, sha="c" * 64)
    state["output"]["messages"][0]["additional_kwargs"]["apx_data_parts"][0]["metadata"]["sha256"] = "d" * 64
    submit(page)
    expect(page.locator("details.native-step-output")).to_have_count(0)


def test_native_step_output_is_bounded(browser_chat):
    page, posts, reads, state, event = browser_chat
    _typed_step(event, state, {"rows": list(range(500)), "deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}})
    submit(page)
    text = page.locator("details.native-step-output > .native-tree").text_content()
    assert "rows500items" in text.replace(" ", "")  # counted, not dumped
    rows = page.locator("details.native-step-output summary", has_text="rows")
    page.locator("details.native-step-output > summary").click()
    rows.click()
    expect(page.locator("details.native-step-output > .native-tree")).to_contain_text("… 450 more")  # built on open


def test_native_unknown_child_is_not_success(browser_chat):
    page, posts, reads, state, event = browser_chat
    event.clear()
    event.update(type="agent.child.unknown", id="actual-child", correlation={"parent_invocation_id": "actual-parent",
                 "transport_sender": "fleet", "logical_predecessor": "calibrate"}, transport_status="unknown")
    state.update(status="failed", error="child may still be running")
    submit(page)
    expect(page.locator(".native-execution-card")).to_contain_text("transport unknown")
    expect(page.locator(".native-execution-card")).to_contain_text("not recorded:")
    expect(page.locator(".native-execution-card")).to_contain_text("actual-child (record link unavailable)")
    expect(page.locator(".native-status")).to_contain_text("Failed")
    expect(page.locator(".msg.assistant")).to_have_count(0)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:secret@example.com/api/invocations/child-id",
                                  "https://example.com/api/invocations/wrong-id"])
def test_native_child_links_reject_unsafe_or_mismatched_urls(browser_chat, url):
    page, posts, reads, state, event = browser_chat
    event.clear()
    event.update(type="agent.child.unknown", id="child-id", child_invocation_url=url, transport_status="unknown")
    submit(page)
    card = page.locator(".native-execution-card")
    expect(card).to_contain_text("child-id (record link unavailable)")
    expect(card.locator("a")).to_have_count(0)


def test_native_accepted_then_readback_failure_has_no_success_card(browser_chat):
    page, posts, reads, state, event = browser_chat
    event.clear()
    event.update(type="agent.child.accepted", id="accepted-child")
    state.update(status="failed", error="readback failed")
    submit(page)
    expect(page.locator(".native-status")).to_contain_text("Failed: readback failed")
    expect(page.locator(".native-execution-card")).to_have_count(0)
    expect(page.locator(".msg.assistant")).to_have_count(0)


def test_native_cards_are_bounded_and_replay_identity_is_deduplicated(browser_chat):
    page, posts, reads, state, event = browser_chat
    event.clear()
    event.update(type="agent.step.completed", step='<img src=x onerror="window.injected=1">', location="local",
                 transport_status="completed", schema_validated=True, domain_status="not_ready")
    def replay(route):
        route.fulfill(content_type="text/event-stream", body=''.join(
            f"id: {i}\ndata: {json.dumps(event)}\n\n" for i in range(1, 71)))
    page.route("**/events?after=*", replay)
    submit(page)
    expect(page.locator(".native-execution-card")).to_have_count(64)
    expect(page.locator(".native-event")).to_have_count(64)
    expect(page.locator(".native-execution-card").first).to_contain_text("not recorded: step")
    assert page.evaluate("window.injected") is None
    page.reload()
    expect(page.locator(".native-execution-card")).to_have_count(64)
    expect(page.locator(".native-event")).to_have_count(64)
    assert len(posts) == 1


def test_native_oversized_event_is_explicitly_omitted(browser_chat):
    page, posts, reads, state, event = browser_chat
    event["message"]["content"] = "x" * 9000
    submit(page)
    expect(page.locator(".native-event")).to_have_text(json.dumps(
        {"type": "event.omitted", "detail": "Event exceeds display limit"}, indent=2))
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")


def test_native_output_record_is_bounded_without_lossy_part_extraction(browser_chat):
    page, posts, reads, state, event = browser_chat
    state["output"]["messages"] = [{"type": "ai", "content": "Typed output", "additional_kwargs": {
        "apx_data_parts": [{"kind": "data", "data": {"index": index}}]}} for index in range(20)]
    submit(page)
    expect(page.locator(".native-output")).to_have_count(1)
    assert json.loads(page.locator(".native-output").text_content())["output"] == state["output"]


def test_native_oversized_output_record_has_explicit_limit(browser_chat):
    page, posts, reads, state, event = browser_chat
    state["output"]["messages"][0]["additional_kwargs"] = {"apx_data_parts": [
        {"kind": "data", "data": {"oversized": "x" * 131073}}]}
    submit(page)
    expect(page.locator(".native-output")).to_have_count(1)
    expect(page.locator(".native-output")).to_have_text("Recorded response exceeds display limit; retrieve the invocation record.")


@pytest.mark.parametrize("failure", [401, 403, 404, "network"])
def test_reload_does_not_show_inaccessible_cached_content(browser_chat, failure):
    page, posts, reads, state, event = browser_chat
    submit(page)
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    def inaccessible(route):
        if failure == "network":
            route.abort("failed")
        else:
            route.fulfill(status=failure, json={"detail": "Not available"})
    page.route("**/api/invocations/*", inaccessible)
    page.reload()
    expect(page.locator(".native-status")).to_contain_text("Connection uncertain")
    expect(page.locator(".msg.assistant")).to_have_count(0)
    expect(page.locator(".msg.user")).to_have_count(0)
    expect(page.locator(".native-event")).to_have_count(0)
    assert len(posts) == 1
    retry = page.get_by_role("button", name="Retry same request", exact=True)
    retry.dispatch_event("click")
    expect(retry).to_be_disabled()
    expect(page.locator(".msg.user")).to_have_count(0)
    expect(page.locator(".native-event")).to_have_count(0)
    assert len(posts) == 1
    old_id = posts[0]["id"]
    page.unroute("**/api/invocations/*", inaccessible)
    event["message"]["content"] = "new run event"
    state["output"]["messages"][0]["content"] = "New answer"
    submit(page, "new user question")
    expect(page.locator(".msg.assistant")).to_have_text("New answer")
    expect(page.locator(".msg.user")).to_have_text("new user question")
    expect(page.locator(".native-event")).to_have_count(1)
    expect(page.locator(".native-event")).to_contain_text("new run event")
    assert len(posts) == 2 and posts[1]["id"] != old_id
    assert posts[1]["input"] == {"messages": [{"role": "user", "content": "new user question"}]}


@pytest.mark.parametrize("surface", ["/", "/_apx/chat"])
def test_browser_real_native_host(request, monkeypatch, surface):
    monkeypatch.setenv("APX_DEV_UI", "1")
    # The dev page loads /_apx/workspace-context; give the fake client real strings (as
    # test_native_server_mounts_apx_chat_and_dev_ui does) so its response validates.
    ws = request.getfixturevalue("execution").ws
    ws.config.host = "https://example.cloud.databricks.com"
    ws.current_user.me.return_value.user_name = "dev@example.com"
    host = request.getfixturevalue("native")
    posts = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
        print("Chromium", browser.version)
        page = browser.new_page()

        def forward(route):
            req = route.request
            url = urlsplit(req.url)
            path = url.path + ("?" + url.query if url.query else "")
            if req.method == "POST":
                posts.append({"path": path, "body": req.post_data_json})
            response = host.client.request(req.method, path, content=req.post_data,
                                           headers={**headers(), "Content-Type": "application/json"})
            route.fulfill(status=response.status_code, body=response.content,
                          headers={"content-type": response.headers.get("content-type", "text/plain")})

        page.route("**/*", forward)
        page.goto("http://localhost" + surface)
        submit(page)
        expect(page.locator(".msg.assistant")).to_have_text("Recorded proof", timeout=30000)
        assert len(posts) == 1 and posts[0]["path"] == "/api/invocations"
        assert host.effects == ["proof"]
        page.reload()
        expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
        assert len(posts) == 1 and host.effects == ["proof"]
        assert "contract-credential-sentinel" not in page.evaluate("JSON.stringify(sessionStorage)")
        browser.close()


def test_native_quick_replies_send_their_text(browser_chat):
    page, posts, reads, state, event = browser_chat
    state["output"]["messages"] = [{"type": "ai", "content": "Ready.",
                                    "additional_kwargs": {"apx_answer": True, "apx_replies": ["approve", "<b>x</b>"]}}]
    submit(page)
    buttons = page.locator(".native-quick-replies button")
    expect(buttons).to_have_count(2)
    expect(buttons.nth(1)).to_have_text("<b>x</b>")  # text, never markup
    assert page.locator(".native-quick-replies b").count() == 0
    buttons.first.click()
    expect(page.locator(".msg.user").last).to_have_text("approve")
    assert len(posts) == 2 and posts[1]["input"]["messages"][-1]["content"] == "approve"


def test_native_answer_without_replies_has_no_buttons(browser_chat):
    page, posts, reads, state, event = browser_chat
    state["output"]["messages"] = [{"type": "ai", "content": "Done.", "additional_kwargs": {"apx_answer": True}}]
    submit(page)
    expect(page.locator(".native-quick-replies")).to_have_count(0)


def test_native_old_quick_replies_are_disabled_after_a_new_message(browser_chat):
    page, posts, reads, state, event = browser_chat
    state["output"]["messages"] = [{"type": "ai", "content": "Ready.",
                                    "additional_kwargs": {"apx_answer": True, "apx_replies": ["approve"]}}]
    submit(page)
    expect(page.locator(".native-quick-replies button")).to_have_count(1)
    state["output"]["messages"] = [{"type": "ai", "content": "Next.", "additional_kwargs": {"apx_answer": True}}]
    submit(page, "other")
    expect(page.locator(".msg.assistant").last).to_have_text("Next.")
    old = page.locator(".native-quick-replies button")
    assert old.count() == 0 or old.first.is_disabled()
    if old.count():
        old.first.click(force=True)
    assert len(posts) == 2


def test_native_typed_input_hosts_show_no_quick_replies(browser_chat):
    page, posts, reads, state, event = browser_chat
    state["input_schema"] = BrowserInput.model_json_schema()
    state["output"]["messages"] = [{"type": "ai", "content": "Ready.",
                                    "additional_kwargs": {"apx_answer": True, "apx_replies": ["approve"]}}]
    page.reload()
    expect(page.locator(".native-input-schema")).to_contain_text("as_of")
    submit(page, json.dumps({"amount": 4, "as_of": "2026-10-22"}))
    expect(page.locator(".msg.assistant")).to_have_text("Ready.")
    expect(page.locator(".native-quick-replies")).to_have_count(0)


@pytest.mark.parametrize("browser_chat", ["dev"], indirect=True)  # only /_apx/chat has the Events panel
def test_native_run_fills_the_events_timeline(browser_chat):
    # The durable event stream feeds the Events tab: labelled, filterable, and never parsed as markup.
    page, posts, reads, state, event = browser_chat
    stream = [
        {"type": "agent.message.delta", "message": {"type": "ai", "content": "", "tool_calls": [
            {"id": "c1", "name": "lookup", "args": {"q": "rates"}}]}},
        {"type": "agent.message.delta", "message": {"type": "tool", "name": "lookup", "tool_call_id": "c1",
                                                    "status": "success", "content": "<img src=x onerror=alert(1)>"}},
        {"type": "agent.step.completed", "step": "calibrate", "transport_status": "completed"},
        {"type": "agent.message.delta", "message": {"type": "ai", "content": "Recorded proof"}},
    ]
    body = "".join(f"id: {i}\nevent: {e['type']}\ndata: {json.dumps(e)}\n\n" for i, e in enumerate(stream, 1))
    page.route("**/events?after=*", lambda route: route.fulfill(content_type="text/event-stream", body=body))
    expect(page.locator(".panel-tabs button", has_text="Events")).to_be_visible()
    expect(page.locator(".panel-tabs button", has_text="Trace")).to_be_hidden()  # Trace still reads the legacy hooks
    submit(page)
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    page.locator(".panel-tabs button", has_text="Events").click()
    titles = page.locator("#events-list .event .event-title")
    expect(titles).to_have_text(["user", "tool call · lookup", "tool response · lookup", "step · calibrate", "assistant"])
    assert page.locator("#events-list img").count() == 0  # event text is never markup
    expect(page.locator("#events-list .event", has_text="tool response")).to_contain_text("<img src=x")
    page.locator("#events-filter button[data-filter=tools]").click()
    expect(page.locator("#events-list .event:visible .event-title")).to_have_text(["tool call · lookup", "tool response · lookup"])
    page.locator("#events-filter button[data-filter=all]").click()
    expect(page.locator("#events-list .event:visible")).to_have_count(5)
    page.reload()  # a restored run rebuilds the same timeline, user turn first
    expect(page.locator(".msg.assistant")).to_have_text("Recorded proof")
    page.locator(".panel-tabs button", has_text="Events").click()
    expect(titles).to_have_text(["user", "tool call · lookup", "tool response · lookup", "step · calibrate", "assistant"])


@pytest.mark.parametrize("browser_chat", ["dev"], indirect=True)
def test_native_sequence_answer_closes_the_events_timeline(browser_chat):
    # A sequence streams only step completions; its declared answer still ends the timeline, once.
    page, posts, reads, state, event = browser_chat
    state["output"]["messages"] = [{"type": "ai", "content": "Ready for approval", "additional_kwargs": {"apx_answer": True}}]
    body = f"id: 1\nevent: agent.step.completed\ndata: {json.dumps({'type': 'agent.step.completed', 'step': 'summary'})}\n\n"
    page.route("**/events?after=*", lambda route: route.fulfill(content_type="text/event-stream", body=body))
    submit(page)
    expect(page.locator(".msg.assistant")).to_contain_text("Ready for approval")
    page.locator(".panel-tabs button", has_text="Events").click()
    expect(page.locator("#events-list .event .event-title")).to_have_text(["user", "step · summary", "assistant"])
    expect(page.locator("#events-list .event").last).to_contain_text("Ready for approval")
    assert page.locator("#events-list").evaluate("e => getComputedStyle(e).textAlign") != "center"  # rows read left-aligned
    page.reload()
    expect(page.locator(".msg.assistant")).to_contain_text("Ready for approval")
    page.locator(".panel-tabs button", has_text="Events").click()
    expect(page.locator("#events-list .event .event-title")).to_have_text(["user", "step · summary", "assistant"])
