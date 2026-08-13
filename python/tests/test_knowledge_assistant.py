"""Tests for ``knowledge_assistant_tool`` — OBO wiring, response parsing, and
composition into a ``SequentialAgent`` flow (PRD AC-1..AC-4)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from apx_agent import knowledge_assistant_tool
from apx_agent._defaults import DatabricksAppsHeaders, UserClientDependency, _get_user_client
from apx_agent._inspection import _inspect_tool_fn


# ---------------------------------------------------------------------------
# Fake KA serving response
# ---------------------------------------------------------------------------

def _make_ka_response(*, answer: str, citations: list[dict[str, Any]] | None = None) -> MagicMock:
    """Build a fake ``QueryEndpointResponse`` matching the serving-query shape."""
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=answer))]
    resp.citations = citations
    return resp


def _make_ws(response: MagicMock) -> MagicMock:
    ws = MagicMock(name="ws")
    ws.serving_endpoints.query.return_value = response
    return ws


# ===========================================================================
# Factory shape
# ===========================================================================

class TestKnowledgeAssistantFactory:
    def test_returns_callable(self):
        assert callable(knowledge_assistant_tool("ka-endpoint"))

    def test_default_name(self):
        assert knowledge_assistant_tool("ka-endpoint").__name__ == "ask_knowledge_assistant"

    def test_custom_name(self):
        assert knowledge_assistant_tool("ka", name="ask_10k").__name__ == "ask_10k"

    def test_default_description_mentions_endpoint(self):
        doc = knowledge_assistant_tool("ka-tenk").__doc__
        assert doc and "ka-tenk" in doc

    def test_inspection_exposes_question_only(self):
        plain, deps = _inspect_tool_fn(knowledge_assistant_tool("ka"))
        assert list(plain.keys()) == ["question"]
        assert deps == ["ws"]


# ===========================================================================
# AC-1 — OBO: an incoming forwarded token yields a user-scoped client
# ===========================================================================

class TestOboUsesUserToken:
    def test_ka_tool_ws_param_uses_obo_dependency(self):
        """The KA tool's ``ws`` resolves through the OBO ``_get_user_client``
        path — no bespoke auth — so it inherits the framework decision."""
        assert UserClientDependency.__metadata__[0].dependency is _get_user_client

    def test_obo_uses_user_token(self):
        """AC-1: with X-Forwarded-Access-Token present, the resolved client is
        built from the *user* token (auth_type='pat'), not SP creds."""
        headers = DatabricksAppsHeaders(
            host="ws.cloud.databricks.com",
            user_name="alice", user_id="1", user_email="a@x.com",
            request_id=None, token=SecretStr("obo-user-token"),
        )
        with patch("apx_agent._defaults.WorkspaceClient") as MockWS, \
             patch("apx_agent._defaults.Config") as MockConfig:
            _get_user_client(headers)
            kwargs = MockConfig.call_args.kwargs
            assert kwargs["auth_type"] == "pat"
            assert kwargs["token"] == "obo-user-token"
            MockWS.assert_called_once_with(config=MockConfig.return_value)


# ===========================================================================
# AC-2 — no token: CLI/SP fallback locally, fail closed in the Apps runtime
# ===========================================================================

class TestNoTokenFallback:
    def test_no_token_fallback(self):
        """Local dev (no DATABRICKS_APP_NAME): falls back to CLI creds — a
        Config built without an OBO token."""
        headers = DatabricksAppsHeaders(
            host=None, user_name=None, user_id=None,
            user_email=None, request_id=None, token=None,
        )
        with patch("apx_agent._defaults.WorkspaceClient") as MockWS, \
             patch("apx_agent._defaults.Config") as MockConfig:
            _get_user_client(headers)
            MockConfig.assert_called_once_with(retry_timeout_seconds=120)
            MockWS.assert_called_once_with(config=MockConfig.return_value)

    def test_no_token_fails_closed_in_apps(self, monkeypatch):
        """Apps runtime (DATABRICKS_APP_NAME set), no token, no SP opt-in: fail
        closed rather than silently running as the app service principal."""
        from apx_agent._obo import ApxIdentityError

        monkeypatch.setenv("DATABRICKS_APP_NAME", "my-app")
        monkeypatch.delenv("APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK", raising=False)
        headers = DatabricksAppsHeaders(
            host=None, user_name=None, user_id=None,
            user_email=None, request_id=None, token=None,
        )
        with pytest.raises(ApxIdentityError):
            _get_user_client(headers)


# ===========================================================================
# AC-3 — grounded-result parsing + error degradation
# ===========================================================================

class TestKaResponseParsing:
    @pytest.mark.asyncio
    async def test_ka_response_parsing(self):
        """Parses answer text + a citations list where each entry carries a
        source ``doc_uri``."""
        resp = _make_ka_response(
            answer="Apple's FY2023 revenue was $383B.",
            citations=[
                {"doc_uri": "s3://filings/AAPL-10-K-2023.pdf", "chunk_id": "c1", "text": "…"},
                {"source": "s3://filings/AAPL-10-K-2022.pdf"},  # alt field name
            ],
        )
        tool = knowledge_assistant_tool("ka-10k")
        result = await tool(question="Apple FY2023 revenue?", ws=_make_ws(resp))

        assert result["question"] == "Apple FY2023 revenue?"
        assert "383B" in result["answer"]
        assert len(result["citations"]) == 2
        assert all(c["doc_uri"] for c in result["citations"])
        assert result["citations"][1]["doc_uri"] == "s3://filings/AAPL-10-K-2022.pdf"

    @pytest.mark.asyncio
    async def test_error_degrades_not_raises(self):
        """A KA/API failure degrades to {'error': …}, never a raise."""
        ws = MagicMock()
        ws.serving_endpoints.query.side_effect = RuntimeError("endpoint not ONLINE")
        tool = knowledge_assistant_tool("ka-10k")
        result = await tool(question="anything", ws=ws)
        assert "error" in result
        assert "endpoint not ONLINE" in result["error"]
        assert result["question"] == "anything"

    @pytest.mark.asyncio
    async def test_no_citations_yields_empty_list(self):
        resp = _make_ka_response(answer="ungrounded", citations=None)
        tool = knowledge_assistant_tool("ka")
        result = await tool(question="q", ws=_make_ws(resp))
        assert result["citations"] == []


# ===========================================================================
# AC-4 — KA tool as a SequentialAgent stage; output feeds the next stage
# ===========================================================================

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from apx_agent import Agent, SequentialAgent  # noqa: E402
from apx_agent import _compile  # noqa: E402
from apx_agent._compile import compile_to_langgraph  # noqa: E402


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class TestKaSubagentInFlow:
    @pytest.mark.asyncio
    async def test_ka_subagent_in_flow(self, monkeypatch):
        """AC-4: stage 0's LlmAgent calls the KA tool; its grounded output is a
        ToolMessage the pipeline reasons over, and stage 1 (next) still runs."""
        model = _ToolFake(messages=iter([
            AIMessage(content="", tool_calls=[
                {"name": "ask_knowledge_assistant", "args": {"question": "Apple revenue?"}, "id": "t1"},
            ]),
            AIMessage(content="stage 0: grounded answer captured"),
            AIMessage(content="stage 1 summary complete"),
        ]))
        monkeypatch.setattr(
            _compile, "_build_chat_databricks",
            lambda endpoint, *, temperature=None, max_tokens=None: model,
        )

        ws = _make_ws(_make_ka_response(
            answer="Apple's FY2023 revenue was $383B.",
            citations=[{"doc_uri": "s3://filings/AAPL-10-K-2023.pdf"}],
        ))
        ws.config.host = "https://fake.cloud.databricks.com"

        stage0 = Agent(name="research", tools=[knowledge_assistant_tool("ka-10k")],
                       instructions="Answer from the knowledge assistant.")
        stage1 = Agent(name="summarize", instructions="Summarize.")
        graph = compile_to_langgraph(SequentialAgent([stage0, stage1]), ws=ws, model="m")

        result = graph.invoke({"messages": [HumanMessage(content="Apple FY2023 revenue?")]})

        # The KA tool ran and its grounded output (with the 10-K citation) is in
        # the transcript as a ToolMessage.
        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert tool_msgs, "KA tool did not run in the flow"
        assert "AAPL-10-K-2023.pdf" in str(tool_msgs[0].content)

        # The pipeline reached the next stage.
        texts = [str(m.content) for m in result["messages"] if isinstance(m, AIMessage)]
        assert any("stage 1 summary complete" in t for t in texts), (
            "flow did not reach stage 1 after the KA stage"
        )
