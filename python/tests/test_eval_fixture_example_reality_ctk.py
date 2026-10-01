"""The shipped example and JSON fixtures execute through the real compiler."""

import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from apx_agent import _compile, evaluate_chain


def test_shipped_fixture_example_replays_success_and_unavailability(monkeypatch, tmp_path):
    source = Path(__file__).parents[1] / "examples/data-triage-agent/eval/replay_example.py"
    spec = importlib.util.spec_from_file_location("fixture_example", source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    def call(customer):
        return AIMessage(content="", tool_calls=[{
            "name": "lookup_customer", "args": {"customer_id": customer}, "id": customer,
        }])

    replies = [
        AIMessage(content='{"customer_id":"EXAMPLE-001"}'), call("EXAMPLE-001"),
        AIMessage(content='{"customer_id":"EXAMPLE-001","exists":true}'),
        AIMessage(content='{"customer_id":"EXAMPLE-001","status":"found"}'),
        AIMessage(content='{"customer_id":"EXAMPLE-002"}'), call("EXAMPLE-002"),
    ]
    model = Model(messages=iter(replies))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)
    report = evaluate_chain(module.build_agent(), model="fake", fixtures=module.load_fixtures())
    path = tmp_path / "report.json"
    path.write_text(json.dumps(asdict(report)))
    saved = json.loads(path.read_text())
    assert saved["outcome_counts"] == {"correct": 1, "escalated_with_evidence": 1, "wrong": 0}
    first, second = saved["cases"]
    assert all(step["passed"] for step in first["steps"])
    assert second["steps"][0]["actual_output"] == {"customer_id": "EXAMPLE-002"}
    packet = json.loads(second["response"])
    assert packet["failed_step"] == ["triage", "lookup"]
    assert packet["evidence"] == [{"step": ["triage", "identify"], "output_key": "customer", "data": {"customer_id": "EXAMPLE-002"}}]
