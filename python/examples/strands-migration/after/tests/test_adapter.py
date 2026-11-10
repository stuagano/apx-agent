"""Offline check: the adapter streams, returns the reply, and keeps multi-turn history. No network."""

import asyncio
from types import SimpleNamespace

from strands.models.model import Model

from runtime import adapter, history
from support_agent.agent import build_agent


class FakeModel(Model):
    """Stands in for OpenAIModel: answers with a fixed text, and records how much history it saw."""

    def __init__(self):
        self.seen = []

    def update_config(self, **_):
        pass

    def get_config(self):
        return {}

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, *args, **kwargs):
        self.seen.append(len(messages))
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": "Order A100 can be refunded."}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def test_two_turns_keep_history(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(adapter, "databricks_model", lambda: model)
    history._local.clear()
    emitted = []

    async def emit(event):
        emitted.append(event)

    ctx = SimpleNamespace(session_id="s1", invocation_id="i1", emit=emit)

    first = asyncio.run(adapter.invoke({"prompt": "Can I refund A100?"}, ctx))
    asyncio.run(adapter.invoke({"prompt": "And A200?"}, ctx))

    assert first["result"] == "Order A100 can be refunded."
    assert any(e["type"] == "delta" for e in emitted)
    assert model.seen == [1, 3]  # turn 2 saw turn 1's user + assistant messages plus the new prompt
    assert len(history._local["s1"]) == 4


def test_tools_unchanged():
    agent = build_agent(FakeModel())
    assert set(agent.tool_names) == {"lookup_order", "refund_eligible"}


def test_fmapi_list_content_flattened():
    from runtime.model import FMAPIModel

    model = FMAPIModel(client_args={"api_key": "x"}, model_id="m")
    parts = [{"type": "text", "text": "Hel"}, {"type": "text", "text": "lo"}]
    chunk = model.format_chunk({"chunk_type": "content_delta", "data_type": "text", "data": parts})
    assert chunk["contentBlockDelta"]["delta"]["text"] == "Hello"
