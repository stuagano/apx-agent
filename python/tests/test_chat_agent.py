"""Tests for _chat_agent.py — MLflow ChatAgent wrapping.

Verifies that:

  1. ``chat_agent_for`` returns an mlflow.pyfunc.ChatAgent subclass.
  2. ``predict`` accepts ChatAgentMessage list, routes through the compile path,
     and returns a ChatAgentResponse with only NEW messages (input is not echoed).
  3. ``predict_stream`` yields ChatAgentChunk per langchain message produced.
  4. **User-scope auth is preserved**: when ``custom_inputs["user_token"]`` is
     present, the compiled graph's tools see the OBO WorkspaceClient — not the
     default SP client. This is the load-bearing assertion.
  5. Without ``user_token``, falls back to the default auth chain (SP via
     oauth-m2m env vars, or CLI in local dev).

Skips if optional extras (``langgraph``, ``eval``) are not installed.
"""

from __future__ import annotations

import textwrap
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")


from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from mlflow.pyfunc import ChatAgent  # noqa: E402
from mlflow.types.agent import (  # noqa: E402
    ChatAgentMessage,
    ChatAgentResponse,
    ChatAgentChunk,
)

from apx_agent import Agent, AgentConfig, AgentContext, LlmAgent, chat_agent_for  # noqa: E402
from apx_agent._resources import collect_resource_specs  # noqa: E402
from apx_agent._wiring import finalize_agent  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trivial_tool(query: str) -> str:
    """A tool with no dependencies (compile-friendly without mocks)."""
    return f"got: {query}"


def _make_fake_graph(final_text: str = "done") -> MagicMock:
    """A fake compiled graph whose invoke() appends an AIMessage to messages."""
    graph = MagicMock(name="fake_compiled_graph")

    def _invoke(state: dict[str, Any]) -> dict[str, Any]:
        return {"messages": [*state["messages"], AIMessage(content=final_text)]}

    def _stream(state: dict[str, Any], stream_mode: str = "updates"):
        # Emit one "node update" with one new AI message.
        yield {"reporter": {"messages": [AIMessage(content=final_text)]}}

    graph.invoke.side_effect = _invoke
    graph.stream.side_effect = _stream
    return graph


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestChatAgentFactory:
    def test_returns_mlflow_chat_agent_subclass(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        wrapped = chat_agent_for(agent, model="databricks-claude-sonnet-4-6")
        assert isinstance(wrapped, ChatAgent)


class TestPredict:
    def test_returns_chat_agent_response_with_only_new_messages(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        wrapped = chat_agent_for(agent, model="any-endpoint")

        fake_graph = _make_fake_graph(final_text="final answer")
        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._chat_agent.compile_to_langgraph",
            return_value=fake_graph,
        ):
            resp = wrapped.predict(
                messages=[ChatAgentMessage(role="user", content="hi", id="u1")]
            )

        assert isinstance(resp, ChatAgentResponse)
        # Input ("hi") must NOT be echoed; only the new assistant message.
        assert len(resp.messages) == 1
        assert resp.messages[0].role == "assistant"
        assert resp.messages[0].content == "final answer"


class TestPredictStream:
    def test_yields_chunks_per_message(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        wrapped = chat_agent_for(agent, model="any")

        fake_graph = _make_fake_graph(final_text="streamed!")
        with patch(
            "apx_agent._defaults._make_workspace_client",
            return_value=MagicMock(name="sp_ws"),
        ), patch(
            "apx_agent._chat_agent.compile_to_langgraph",
            return_value=fake_graph,
        ):
            chunks = list(
                wrapped.predict_stream(
                    messages=[ChatAgentMessage(role="user", content="go", id="u1")]
                )
            )

        assert len(chunks) == 1
        assert isinstance(chunks[0], ChatAgentChunk)
        assert chunks[0].delta.content == "streamed!"
        assert chunks[0].delta.role == "assistant"


class TestUserScopeAuth:
    """The load-bearing test: user_token in custom_inputs MUST flow to compile."""

    def test_user_token_builds_obo_workspace_client(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        wrapped = chat_agent_for(agent, model="any")

        fake_graph = _make_fake_graph()
        captured: dict[str, Any] = {}

        def _spy_compile(agent_arg, *, ws, model, headers=None):
            captured["ws"] = ws
            captured["model"] = model
            return fake_graph

        # Patch _make_workspace_client where _chat_agent imports it: inside
        # _resolve_ws_for_request, which uses ``from ._defaults import ...``.
        with patch(
            "apx_agent._defaults._make_workspace_client"
        ) as mock_factory, patch(
            "apx_agent._chat_agent.compile_to_langgraph",
            side_effect=_spy_compile,
        ):
            sentinel_ws = MagicMock(name="obo_ws")
            mock_factory.return_value = sentinel_ws

            wrapped.predict(
                messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
                custom_inputs={
                    "user_token": "tok-abc",
                    "workspace_host": "https://fake.cloud.databricks.com",
                },
            )

        # The factory was called with the OBO token — i.e. the closure-based
        # user-scope auth made it from custom_inputs all the way to the
        # WorkspaceClient construction.
        mock_factory.assert_called_once_with(
            token="tok-abc",
            host="https://fake.cloud.databricks.com",
        )
        # And the ws handed to compile_to_langgraph IS the OBO one.
        assert captured["ws"] is sentinel_ws

    def test_no_user_token_falls_back_to_default(self) -> None:
        agent = LlmAgent(tools=[_trivial_tool])
        wrapped = chat_agent_for(agent, model="any")

        fake_graph = _make_fake_graph()

        with patch(
            "apx_agent._defaults._make_workspace_client"
        ) as mock_factory, patch(
            "apx_agent._chat_agent.compile_to_langgraph",
            return_value=fake_graph,
        ):
            mock_factory.return_value = MagicMock(name="sp_ws")
            wrapped.predict(
                messages=[ChatAgentMessage(role="user", content="hi", id="u1")],
                custom_inputs=None,
            )

        # No kwargs — default SP/CLI path.
        mock_factory.assert_called_once_with()


# --- _from_langchain_message conversion (model-serving deploy regressions) ---

def test_from_langchain_message_serializes_tool_call_arguments() -> None:
    """AIMessage tool-call args (a dict) must become a JSON *string* for
    ChatAgentMessage. Regression: model-serving deploy failed at log_agent's
    input-example validation with `arguments` as a dict."""
    import json
    from apx_agent._chat_agent import _from_langchain_message

    msg = AIMessage(
        content="",
        tool_calls=[{"id": "call_1", "name": "run_sql",
                     "args": {"query": "SELECT 1"}}],
    )
    dumped = _from_langchain_message(msg, 0).model_dump()
    arguments = dumped["tool_calls"][0]["function"]["arguments"]
    assert isinstance(arguments, str)
    assert json.loads(arguments) == {"query": "SELECT 1"}


def test_from_langchain_message_tool_message_has_name_and_id() -> None:
    """ToolMessage -> ChatAgentMessage must carry both name and tool_call_id;
    ChatAgentMessage rejects tool messages missing either."""
    from langchain_core.messages import ToolMessage
    from apx_agent._chat_agent import _from_langchain_message

    out = _from_langchain_message(
        ToolMessage(content="result", tool_call_id="call_1", name="run_sql"), 1
    )
    assert out.role == "tool"
    assert out.name == "run_sql"
    assert out.tool_call_id == "call_1"


def test_from_langchain_message_tool_message_name_fallback() -> None:
    """A nameless ToolMessage still satisfies ChatAgentMessage via a fallback."""
    from langchain_core.messages import ToolMessage
    from apx_agent._chat_agent import _from_langchain_message

    out = _from_langchain_message(ToolMessage(content="r", tool_call_id="c2"), 2)
    assert out.name  # non-empty fallback
    assert out.tool_call_id == "c2"


def test_chat_msg_to_new_items_keeps_assistant_text_with_tool_calls() -> None:
    """#493: an assistant turn with both prose and tool calls persists the prose
    as a message item too — not just the function_call items."""
    from apx_agent._chat_agent import _chat_msg_to_new_items

    msg = ChatAgentMessage(
        role="assistant",
        content="Let me check the database.",
        tool_calls=[{
            "id": "call_1", "type": "function",
            "function": {"name": "run_sql", "arguments": "{}"},
        }],
        id="a1",
    )
    items = _chat_msg_to_new_items(msg, model="m", response_id="r1")
    types = [it.type for it in items]
    # Prose preserved (message) alongside the call, prose first.
    assert types == ["message", "function_call"]
    msg_item = next(it for it in items if it.type == "message")
    assert msg_item.data.content[0]["text"] == "Let me check the database."


def test_chat_msg_to_new_items_no_empty_message_without_text() -> None:
    """A tool-call turn with no prose stores only the function_call (no empty msg)."""
    from apx_agent._chat_agent import _chat_msg_to_new_items

    msg = ChatAgentMessage(
        role="assistant",
        content="",
        tool_calls=[{
            "id": "c1", "type": "function",
            "function": {"name": "f", "arguments": "{}"},
        }],
        id="a2",
    )
    items = _chat_msg_to_new_items(msg, model="m", response_id="r1")
    assert [it.type for it in items] == ["function_call"]


# --- T6: finalize_agent wired into log_agent (GOVERNANCE) ---


def test_finalized_agent_contributes_genie_space_resource(tmp_path):
    pp = tmp_path / "pyproject.toml"
    pp.write_text(textwrap.dedent("""
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef0000000000000000000000000000"
        name = "ask_sales"
    """))
    agent = Agent(tools=[])
    finalize_agent(agent, pyproject_path=str(pp))
    kinds = [s.kind for s in collect_resource_specs(agent, model="m")]
    assert "genie_space" in kinds


def test_log_agent_finalizes_before_resource_derivation(tmp_path, monkeypatch):
    # GOVERNANCE: proves finalize_agent runs BEFORE mlflow_resources_for inside
    # log_agent. If that ordering is swapped, config-declared tools won't appear
    # in the logged model's resource list → silent deploy-time permission
    # failures (the bug the sub_agents precedent shipped). The spy captures
    # agent.collect_tools() at the resource-derivation call site; if finalize ran
    # first, "ask_sales" is present. NOTE: mlflow_resources_for is patched on the
    # _resources module (not _chat_agent) because log_agent imports it via a
    # local `from ._resources import ...`, which resolves the name from _resources
    # at call time — patching _chat_agent would not intercept it.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
        [tool.apx.agent]
        name = "t"
        [[tool.apx.tools]]
        type = "genie"
        space_id = "01ef0000000000000000000000000000"
        name = "ask_sales"
    """))
    import sys
    import types
    import apx_agent._resources as res
    import apx_agent._chat_agent as ca

    seen = {}
    def spy_resources(agent, **kw):
        # State of the agent AT the resource-derivation call site.
        seen["tools"] = [t.name for t in agent.collect_tools()]
        return []
    # log_agent does a LOCAL `from ._resources import ... mlflow_resources_for`,
    # so the name resolves from the _resources module → patch it THERE, not on ca.
    monkeypatch.setattr(res, "mlflow_resources_for", spy_resources)
    # compile_to_chat_agent is a module global of _chat_agent → patch on ca.
    monkeypatch.setattr(ca, "compile_to_chat_agent", lambda agent, **kw: object())
    # Stub mlflow so no real logging happens. log_agent does NOT call start_run.
    fake_pyfunc = types.SimpleNamespace(log_model=lambda **kw: types.SimpleNamespace(name="m"))
    fake_mlflow = types.SimpleNamespace(pyfunc=fake_pyfunc, set_experiment=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)
    monkeypatch.setitem(sys.modules, "mlflow.pyfunc", fake_pyfunc)

    from apx_agent._chat_agent import log_agent
    log_agent(Agent(tools=[]), model="m")
    assert "ask_sales" in seen["tools"]  # finalize ran BEFORE resource derivation


def test_resolve_ws_rejects_tokenless_request_in_app(monkeypatch):
    """G2 wiring: the chat auth chokepoint fails closed in the Apps runtime when
    no OBO token is present (no app-SP fallback unless opted in)."""
    import pytest

    from apx_agent._chat_agent import _resolve_ws_and_headers
    from apx_agent._obo import ApxIdentityError

    monkeypatch.setenv("DATABRICKS_APP_NAME", "my-app")
    monkeypatch.delenv("APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK", raising=False)
    with pytest.raises(ApxIdentityError):
        _resolve_ws_and_headers(custom_inputs=None)


def _resolve_headers(monkeypatch, custom_inputs):
    from apx_agent._chat_agent import _resolve_ws_and_headers

    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_URL", raising=False)
    with patch("apx_agent._defaults._make_workspace_client", return_value=MagicMock()):
        return _resolve_ws_and_headers(custom_inputs=custom_inputs).headers


def test_token_without_user_id_still_builds_forwarding_headers(monkeypatch) -> None:
    """#467: a user_token but no user_id must still yield headers carrying the
    token, else the A2A sub-agent hop launders the caller's privilege away."""
    headers = _resolve_headers(monkeypatch, {"user_token": "tok-xyz"})
    assert headers is not None
    assert headers.token is not None
    assert headers.token.get_secret_value() == "tok-xyz"


def test_user_id_without_token_still_builds_headers(monkeypatch) -> None:
    headers = _resolve_headers(monkeypatch, {"user_id": "u-1"})
    assert headers is not None
    assert headers.token is None
    assert headers.user_id == "u-1"


def test_no_identity_yields_none_headers(monkeypatch) -> None:
    assert _resolve_headers(monkeypatch, {}) is None


def test_chat_landing_renders_workflow_examples_once_and_escapes_text() -> None:
    from html import escape
    from html.parser import HTMLParser

    from apx_agent._models import AgentCard
    from apx_agent._ui_chat import _render_landing

    question = 'What is the <b>position</b>? <script>alert("x")</script> & peers?'
    config = AgentConfig(
        name="demo-agent",
        examples=[question],
        workflows=[{
            "id": "position",
            "title": "<Pricing review>",
            "question": question,
            "purpose": "Compare <b>peers</b>.",
            "route": ["calibrate"],
        }],
    )
    ctx = AgentContext(
        config=config,
        tools=[],
        card=AgentCard(name=config.name, description="", skills=[]),
        agent=None,  # type: ignore[arg-type]
    )

    html = _render_landing(ctx)

    assert escape(question) in html
    assert "<b>position</b>" not in html
    assert "<script>alert(\"x\")</script>" not in html

    class _WorkflowButtonParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.starter_button_count = 0
            self.starter_data_qs: list[str] = []
            self.workflow_data_qs: list[str] = []
            self.workflow_uses_example: list[bool] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            attributes = dict(attrs)
            class_attr = attributes.get("class")
            if tag != "button" or class_attr is None:
                return
            classes = class_attr.split()
            if "starter-chip" not in classes:
                return
            self.starter_button_count += 1
            data_q = attributes.get("data-q")
            if data_q is None:
                return
            self.starter_data_qs.append(data_q)
            if "workflow-chip" in classes:
                self.workflow_data_qs.append(data_q)
                self.workflow_uses_example.append(
                    attributes.get("onclick") == "useExample(this)"
                )

    parser = _WorkflowButtonParser()
    parser.feed(html)
    assert parser.starter_button_count == 1
    assert parser.starter_data_qs == [question]
    assert parser.workflow_data_qs == [question]
    assert parser.workflow_uses_example == [True]
    assert "&lt;Pricing review&gt;" in html
    assert "Compare &lt;b&gt;peers&lt;/b&gt;." in html


def test_chat_landing_renders_attached_workflow_examples_once_and_escapes_text() -> None:
    from html import escape
    from types import SimpleNamespace

    from apx_agent._models import AgentCard
    from apx_agent._ui_chat import _render_landing

    title = "<Attached pricing review>"
    purpose = 'Compare <b>peers</b> & "positioning".'
    question = 'What is the <b>position</b>? <script>alert("x")</script> & peers?'
    workflow = {
        "id": "attached-position",
        "title": title,
        "question": question,
        "purpose": purpose,
        "route": ["calibrate"],
    }
    config = AgentConfig(name="demo-agent")
    ctx = AgentContext(
        config=config,
        tools=[],
        card=AgentCard(name=config.name, description="", skills=[]),
        agent=SimpleNamespace(__apx_workflows__=[workflow]),
    )

    html = _render_landing(ctx)

    data_q = escape(question, quote=True).replace("?", "&#x3f;")
    assert html.count(escape(title)) == 1
    assert html.count(escape(purpose)) == 1
    assert html.count(escape(question)) == 1
    assert "<b>position</b>" not in html
    assert "<script>alert(\"x\")</script>" not in html
    assert (
        'class="starter-chip workflow-chip" onclick="useExample(this)" '
        f'data-q="{data_q}"'
    ) in html
