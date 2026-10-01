"""Fixture evaluation is explicit; live eval arguments cannot be silently ignored."""

import pytest

from apx_agent import Agent, evaluate_chain


@pytest.mark.parametrize("live", [
    {"evalset": []}, {"experiment": "/experiment"}, {"scorers": []},
    {"user_token": "not-a-real-token"}, {"workspace_host": "https://example.test"},
    {"lookback_traces": 100},
])
def test_fixture_mode_rejects_live_eval_options(live):
    with pytest.raises(ValueError, match="fixtures"):
        evaluate_chain(Agent(), model="unused", fixtures=[], **live)


def test_live_mode_requires_evalset_and_experiment():
    with pytest.raises(ValueError, match="evalset.*experiment"):
        evaluate_chain(Agent(), model="unused")


def test_compiler_records_validated_nested_steps_without_changing_policy(monkeypatch):
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from pydantic import BaseModel

    from apx_agent import SequentialAgent, _compile

    class Output(BaseModel):
        value: int

    class Recorder:
        def __init__(self):
            self.outputs = []

        def middleware(self, path):
            return AgentMiddleware()

        def record(self, path, payload):
            self.outputs.append((path, payload))

    model = GenericFakeChatModel(messages=iter([AIMessage(content='{"value":1}')]))
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **kw: model)
    root = SequentialAgent([
        SequentialAgent([Agent(name="leaf", output_schema=Output)], name="nested"),
    ], name="root")
    observer = Recorder()
    ctx = _compile.CompileContext(service_ws=None, user_ws=None, model="fake", fixture_replay=observer)
    result = _compile._compile_any(root, ctx).invoke({"messages": [HumanMessage(content="go")]})
    assert observer.outputs == [(("root", "nested", "leaf"), {"value": 1})]
    assert root._on_failure == "raise"
    assert result.get("_apx_escalation") is None
