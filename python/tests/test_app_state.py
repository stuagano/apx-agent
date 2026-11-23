"""Deploy-time app: constants (#1233).

The declaration is app_state on the agent (YAML app_state: is written onto
that agent). Every invoke seeds app:<key>. A runtime write does not replace it.
Doctor and agents describe list key names, not values.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from apx_agent import Agent, Dependencies, SequentialAgent  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._app_state import (  # noqa: E402
    check_app_constants,
    graph_input_with_app_state,
    normalize_app_state,
)
from apx_agent._compile import compile_to_langgraph  # noqa: E402
from apx_agent._graph_turn import run_graph_turn  # noqa: E402
from apx_agent._models import AgentConfig  # noqa: E402
from apx_agent._project_gen import render_agent_py  # noqa: E402
from apx_agent._settings import resolve_settings  # noqa: E402
from apx_agent._state_proxy import StateProxy  # noqa: E402


class RecordingModel(BaseChatModel):
    """Captures system prompts the compiled graph sends to the model."""

    seen: ClassVar[list[str]] = []

    @property
    def _llm_type(self) -> str:
        return "app-state-recording"

    def bind_tools(self, tools: Any, **kwargs: Any) -> RecordingModel:
        return self

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any,
    ) -> ChatResult:
        for message in messages:
            if isinstance(message, SystemMessage) and isinstance(message.content, str):
                RecordingModel.seen.append(message.content)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


class _ToolModel(BaseChatModel):
    """First call requests poke; the next call finishes."""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "app-state-tool"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ToolModel:
        return self

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        if self.calls == 1:
            reply = AIMessage(content="", tool_calls=[{"name": "poke", "args": {}, "id": "c1"}])
        else:
            reply = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=reply)])


def _ws() -> Any:
    ws = MagicMock(name="ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    return ws


def _record(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingModel.seen.clear()
    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **k: RecordingModel())
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda **k: _ws())


def _rendered() -> list[str]:
    return [text for text in RecordingModel.seen if text.startswith("Flag is ")]


def test_keys_normalize_with_or_without_the_prefix() -> None:
    assert normalize_app_state({"app:flag": "on", "limit": 2, "ok": True, "nest": {"n": 1}}) == {
        "flag": "on", "limit": 2, "ok": True, "nest": {"n": 1},
    }
    agent = Agent(tools=[], app_state={"app:flag": "on"})
    agent.configure(app_state={"flag": "off", "tags": ["a", None]})
    assert agent.app_state == {"flag": "off", "tags": ["a", None]}


@pytest.mark.parametrize("value", [{"user:flag": "on"}, {"flag": {1, 2}}, {"app:app:flag": "on"}, {"": "on"}])
def test_app_state_rejects_non_identifiers_and_non_json(value: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Agent(tools=[], app_state=value)


def test_duplicate_prefixed_and_bare_key_is_an_error() -> None:
    with pytest.raises(ValueError, match="more than once"):
        Agent(tools=[], app_state={"flag": "on", "app:flag": "off"})


def test_output_key_cannot_target_app_config() -> None:
    with pytest.raises(ValueError, match="app_state"):
        Agent(tools=[], output_key="app:flag")


def test_child_cannot_declare_a_different_app_state() -> None:
    root = SequentialAgent(
        [Agent(name="leaf", tools=[], app_state={"flag": "no"})],
        app_state={"flag": "on"},
    )
    with pytest.raises(ValueError, match="app_state is app-wide"):
        resolve_settings(root, None)


def test_yaml_spec_renders_app_state_onto_the_agent() -> None:
    single = AgentConfig.model_validate({
        "name": "triage",
        "instructions": "Flag is {app:flag}.",
        "app_state": {"app:flag": "on", "limits": {"n": 2}},
    })
    source = render_agent_py(single)
    assert "app_state={'flag': 'on', 'limits': {'n': 2}}" in source
    graph = AgentConfig.model_validate({
        "name": "fleet",
        "app_state": {"flag": "on"},
        "agents": {"leaf": {"type": "agent", "instructions": "Flag is {app:flag}."}},
        "root": {"type": "sequential", "agents": ["leaf"]},
    })
    rendered = render_agent_py(graph)
    assert "SequentialAgent" in rendered
    assert "app_state={'flag': 'on'}" in rendered


def test_describe_of_a_plain_object_has_no_app_keys() -> None:
    from types import SimpleNamespace

    from apx_agent._app_state import declared_app_keys

    assert declared_app_keys(SimpleNamespace(_instructions="")) == []


def test_unset_app_state_does_not_change_graph_input() -> None:
    agent = Agent(tools=[])
    graph_input = {"messages": []}
    assert graph_input_with_app_state(agent, graph_input) is graph_input


def test_state_proxy_rejects_an_app_write(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from apx_agent._session_state import _warned_scoped_keys

    _warned_scoped_keys.clear()
    proxy = StateProxy({"app:flag": "on"})
    with caplog.at_level(logging.WARNING, logger="apx_agent._session_state"):
        with pytest.raises(ValueError, match="app_state"):
            proxy["app:flag"] = "hacked"
    assert proxy.delta == {}
    assert proxy["app:flag"] == "on"
    assert "app:flag" in caplog.text
    with pytest.raises(ValueError, match="app_state"):
        del proxy["app:flag"]


def test_doctor_and_describe_list_keys_not_values(tmp_path: Any) -> None:
    agent = Agent(tools=[], app_state={"flag": "secret-value", "app:limit": 3})
    check = check_app_constants(agent, None, None)
    assert check is not None
    assert check.name == "App constants"
    assert check.detail == "keys: flag, limit (values are not shown)"
    assert "secret-value" not in check.detail

    agent_py = tmp_path / "agent.py"
    agent_py.write_text('agent = Agent(app_state={"flag": "secret-value"})\n')
    static = check_app_constants(None, None, agent_py)
    assert static is not None
    assert static.detail == "keys: flag (values are not shown)"
    assert "secret-value" not in static.detail

    spec = tmp_path / "agent.yaml"
    spec.write_text("name: triage\ninstructions: hi\napp_state:\n  flag: secret-value\n")
    from click.testing import CliRunner

    from apx_agent.cli import info

    result = CliRunner().invoke(info, ["--format", "json", str(spec)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["app_constants"] == ["flag"]
    assert "secret-value" not in result.output


def test_flag_renders_for_two_principals(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mlflow")
    from langgraph.checkpoint.memory import InMemorySaver
    from mlflow.types.agent import ChatAgentMessage

    from apx_agent import chat_agent_for

    _record(monkeypatch)
    agent = Agent(
        name="a", tools=[], instructions="Flag is {app:flag}.", app_state={"app:flag": "on"},
    )
    chat = chat_agent_for(agent, model="m", conversation_store=None, checkpointer=InMemorySaver())
    for user in ("alice", "bob"):
        chat.predict(
            [ChatAgentMessage(role="user", content="hi", id=user)],
            custom_inputs={"session_id": "shared", "user_id": user},
        )
    assert _rendered() == ["Flag is on.", "Flag is on."]


def test_runtime_write_does_not_override_config(monkeypatch: pytest.MonkeyPatch) -> None:
    errors: list[str] = []

    def poke(state: Dependencies.State) -> str:
        """Try to replace the deployed flag."""
        try:
            state["app:flag"] = "hacked"
        except ValueError as exc:
            errors.append(str(exc))
        return str(state["app:flag"])

    monkeypatch.setattr(_compile, "_build_chat_databricks", lambda *a, **k: _ToolModel())
    agent = Agent(
        name="a", tools=[poke], instructions="Flag is {app:flag}.", app_state={"flag": "on"},
    )
    graph = compile_to_langgraph(agent, ws=_ws(), model="m")
    turn = run_graph_turn(
        graph, agent, [HumanMessage(content="go")],
        read_interrupt=lambda graph, config: None,
        domain_input={"app:flag": "from-the-caller", "note": "kept"},
    )
    assert errors and "app_state" in errors[0]
    assert turn.state["app:flag"] == "on"
    assert turn.state["note"] == "kept"


def test_every_invoke_path_seeds(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mlflow")
    from mlflow.types.agent import ChatAgentMessage
    from mlflow.types.responses import ResponsesAgentRequest

    from apx_agent import chat_agent_for
    from apx_agent._responses_agent import compile_to_responses_agent

    _record(monkeypatch)
    agent = Agent(
        name="a", tools=[], instructions="Flag is {app:flag}.", app_state={"flag": "on"},
    )

    chat = chat_agent_for(agent, model="m", conversation_store=None)
    chat.predict(
        [ChatAgentMessage(role="user", content="hi", id="1")],
        custom_inputs={"session_id": "chat", "user_id": "ada"},
    )
    list(chat.predict_stream(
        [ChatAgentMessage(role="user", content="hi", id="2")],
        custom_inputs={"session_id": "chat-stream", "user_id": "ada"},
    ))

    non_stream, streaming = compile_to_responses_agent(agent, model="m", checkpointer=None)
    request = ResponsesAgentRequest(
        input=[{"role": "user", "content": "hi"}],
        custom_inputs={"thread_id": "responses", "user_id": "ada"},
    )
    with patch("apx_agent._defaults._make_workspace_client", return_value=_ws()):
        non_stream(request)
        list(streaming(ResponsesAgentRequest(
            input=[{"role": "user", "content": "hi"}],
            custom_inputs={"thread_id": "responses-stream", "user_id": "ada"},
        )))

    graph = compile_to_langgraph(agent, ws=_ws(), model="m")
    durable = run_graph_turn(
        graph, agent, [HumanMessage(content="hi")], read_interrupt=lambda graph, config: None,
    )
    assert durable.state["app:flag"] == "on"

    leaf = Agent(name="leaf", tools=[], instructions="Flag is {app:flag}.")
    root = SequentialAgent([leaf], app_state={"flag": "on"})
    composite = compile_to_langgraph(root, ws=_ws(), model="m")
    composite_turn = run_graph_turn(
        composite, root, [HumanMessage(content="hi")], read_interrupt=lambda graph, config: None,
    )
    assert composite_turn.state["app:flag"] == "on"

    composite_chat = chat_agent_for(root, model="m", conversation_store=None)
    composite_chat.predict(
        [ChatAgentMessage(role="user", content="hi", id="3")],
        custom_inputs={"session_id": "composite"},
    )
    assert _rendered() == ["Flag is on."] * len(_rendered())
    assert len(_rendered()) >= 6


def test_graph_agent_without_a_state_channel_refuses_app_state() -> None:
    from langgraph.graph import END, START, MessagesState, StateGraph

    from apx_agent import GraphAgent

    def build(tools: list[Any], checkpointer: Any) -> Any:
        graph = StateGraph(MessagesState)
        graph.add_node("n", lambda state: {})
        graph.add_edge(START, "n")
        graph.add_edge("n", END)
        return graph.compile(checkpointer=checkpointer)

    agent = GraphAgent(build=build, name="g", tools=[], app_state={"flag": "on"})
    with pytest.raises(ValueError, match="app_state"):
        compile_to_langgraph(agent, ws=None, model="m")
