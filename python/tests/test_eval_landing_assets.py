"""Eval and judge-alignment scripts are static assets the dev router serves (#1052).

An HTML substring check is not the gate: the scripts run, as separate classic
files, against a DOM. #1011 shipped a page whose string tests passed while the
live script threw.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from ctk import Artifact, verify
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent._dev import build_dev_ui_router
from apx_agent._ui_chat import _render_eval_landing

PYTHON = Path(__file__).resolve().parents[1]
VENDOR = PYTHON / "src/apx_agent/_static/vendor"
EVAL_JS = VENDOR / "eval-landing.js"
JUDGE_JS = VENDOR / "judge-alignment.js"
EVAL_TAG = '<script src="/_apx/vendor/eval-landing.js"></script>'
JUDGE_TAG = '<script src="/_apx/vendor/judge-alignment.js"></script>'

_DOM = r"""
const ids = {};
function el() {
  return {
    innerHTML: "",
    textContent: "",
    value: "",
    disabled: false,
    style: {},
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    async click() { for (const fn of this.listeners.click || []) await fn(); },
    focus() {},
  };
}
for (const id of ["cases", "run-all", "reset", "add-btn", "progress-fill", "status",
                  "add-q", "add-criterion", "la-judge", "la-history", "la-status", "la-align-btn"]) {
  ids[id] = el();
}
const meta = {textContent: ""};
const addSection = {scrollIntoView() { this.scrolled = true; }, scrolled: false};
const windowListeners = {};
global.document = {
  getElementById(id) { return ids[id] || null; },
  querySelector(sel) {
    if (sel === ".meta") return meta;
    if (sel === ".add-section") return addSection;
    return null;
  },
};
global.window = global;
global.addEventListener = (type, fn) => { (windowListeners[type] = windowListeners[type] || []).push(fn); };
global.__APX_EVAL_ROWS__ = [{
  question: "Price our HYSA", expected_judge: "gives options", status: "pending", response: "",
}];
global.__APX_EVAL_NATIVE__ = true;
const calls = [];
let historyRuns = [];
global.fetch = async (url, opts = {}) => {
  const method = opts.method || "GET";
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({url, method, body});
  const reply = (payload, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    statusText: "OK",
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
  if (url === "/_apx/eval/label-history" || url.startsWith("/_apx/eval/label-history?")) {
    return reply({ok: true, runs: historyRuns});
  }
  if (url === "/_apx/eval/label-align" && method === "POST") {
    historyRuns = [{
      start_time: "2026-11-23T15:04:05",
      judge_name: body.judge_name,
      guidelines: ["Be grounded."],
      trace_count: 2,
    }];
    return reply({ok: true, guidelines: ["Be grounded."], trace_count: 2});
  }
  if (url === "/api/invocations" && method === "POST") return reply({id: body.id, status: "active"}, 202);
  if (url.startsWith("/api/invocations/")) {
    return reply({status: "completed", output: {messages: [
      {type: "ai", content: "step chatter"},
      {type: "ai", content: "Options: hold, +5", additional_kwargs: {apx_answer: true}},
    ]}});
  }
  if (url === "/_apx/eval/judge") return reply({ok: true, pass: true, verdict: "PASS", reason: "lists options"});
  if (url === "/_apx/eval/data") return reply({ok: true});
  throw new Error("unexpected " + method + " " + url);
};
process.on("unhandledRejection", (err) => { console.error(err); process.exit(1); });
const vm = require("vm");
const fs = require("fs");
vm.runInThisContext(fs.readFileSync(process.env.EVAL_JS, "utf8"), {filename: "eval-landing.js"});
vm.runInThisContext(fs.readFileSync(process.env.JUDGE_JS, "utf8"), {filename: "judge-alignment.js"});
(async () => {
  await new Promise((done) => setImmediate(done));
  if (!ids.cases.innerHTML.includes("Price our HYSA")) throw new Error("render: " + ids.cases.innerHTML);
  if (!ids["la-history"].innerHTML.includes("No alignments yet.")) {
    throw new Error("history: " + ids["la-history"].innerHTML);
  }
  ids["la-judge"].value = "  ";
  await labelAlign();
  if (ids["la-status"].textContent !== "Enter a judge name (e.g. quality).") {
    throw new Error("empty judge: " + ids["la-status"].textContent);
  }
  await ids["run-all"].click();
  const row = global.__APX_EVAL_ROWS__[0];
  if (row.status !== "pass" || row.response !== "Options: hold, +5") {
    throw new Error("case: " + JSON.stringify(row));
  }
  if (!ids.cases.innerHTML.includes("badge-pass")) throw new Error("badge: " + ids.cases.innerHTML);
  if (ids.status.textContent !== "1/1 passed") throw new Error("progress: " + ids.status.textContent);
  for (const fn of windowListeners.message) fn({data: {type: "apx:table-selected", fqn: "cat.sch.accounts"}});
  await new Promise((done) => setImmediate(done));
  if (global.__APX_EVAL_ROWS__.length !== 5) throw new Error("seeded " + global.__APX_EVAL_ROWS__.length);
  if (ids["add-q"].value !== "Ask about accounts: ") throw new Error("prompt: " + ids["add-q"].value);
  if (!addSection.scrolled) throw new Error("add section did not scroll");
  ids["la-judge"].value = "quality";
  await labelAlign();
  const judged = calls.filter((call) => call.url === "/_apx/eval/judge").map((call) => call.body);
  const posted = calls.filter((call) => call.url === "/api/invocations" && call.method === "POST");
  console.log(JSON.stringify({
    judged,
    session: posted[0] && posted[0].body.session_id,
    background: posted[0] && posted[0].body.background,
    status: ids["la-status"].innerHTML,
    history: ids["la-history"].innerHTML,
    alignDisabled: ids["la-align-btn"].disabled,
  }));
})().catch((err) => { console.error(err); process.exit(1); });
"""


def test_eval_page_loads_vendor_scripts_instead_of_inlining_them():
    html = _render_eval_landing(
        [{"question": "Price our HYSA", "expected_judge": "gives options"}],
        "evals.json",
        None,
        native=True,
    )
    assert html.count(EVAL_TAG) == html.count(JUDGE_TAG) == 1
    assert html.index("window.__APX_EVAL_ROWS__") < html.index(EVAL_TAG) < html.index(JUDGE_TAG)
    assert "window.__APX_EVAL_NATIVE__ = true;" in html
    assert '"question": "Price our HYSA"' in html
    for token in (
        "async function runCase",
        "async function runNative",
        "async function labelAlign",
        "function renderAlignHistory",
        "function loadAlignHistory",
    ):
        assert token not in html
    verify(Artifact(EVAL_JS, min_bytes=1000, must_contain="async function runCase(i)"))
    verify(Artifact(JUDGE_JS, min_bytes=500, must_contain="async function labelAlign()"))


@pytest.mark.asyncio
async def test_eval_assets_are_served_by_the_dev_router():
    app = FastAPI()
    app.state.agent_context = None
    app.include_router(build_dev_ui_router())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get("/_apx/eval")
        eval_js = await client.get("/_apx/vendor/eval-landing.js")
        judge_js = await client.get("/_apx/vendor/judge-alignment.js")
    assert page.status_code == eval_js.status_code == judge_js.status_code == 200
    assert "javascript" in eval_js.headers["content-type"]
    assert "javascript" in judge_js.headers["content-type"]
    assert eval_js.text == EVAL_JS.read_text(encoding="utf-8")
    assert judge_js.text == JUDGE_JS.read_text(encoding="utf-8")
    assert page.text.count(EVAL_TAG) == page.text.count(JUDGE_TAG) == 1
    assert "async function labelAlign" not in page.text
    assert "async function runCase" not in page.text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_eval_and_judge_scripts_run_as_separate_classic_scripts():
    out = subprocess.run(
        ["node", "-e", _DOM],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "EVAL_JS": str(EVAL_JS), "JUDGE_JS": str(JUDGE_JS)},
    )
    report = json.loads(out.stdout)
    assert report["judged"] == [{
        "question": "Price our HYSA",
        "response": "Options: hold, +5",
        "criterion": "gives options",
    }]
    assert report["background"] is True
    uuid_text = report["session"]
    assert isinstance(uuid_text, str) and len(uuid_text) == 36
    assert "Aligned on 2 traces" in report["status"]
    assert "1 guidelines distilled" in report["status"]
    assert "Be grounded." in report["status"]
    assert "Judge aligned 1 time" in report["history"]
    assert "quality" in report["history"]
    assert "2 traces" in report["history"]
    assert "Be grounded." in report["history"]
    assert report["alignDisabled"] is False
