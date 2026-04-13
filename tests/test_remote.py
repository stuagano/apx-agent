"""Tests for RemoteDatabricksAgent and _url_to_app_name."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
import httpx

from apx_agent._remote import RemoteDatabricksAgent, _url_to_app_name
from apx_agent._models import Message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_request(headers: dict[str, str] | None = None) -> MagicMock:
    """Build a minimal FastAPI Request mock."""
    req = MagicMock()
    req.headers = headers or {}
    return req


def make_card_data(
    name: str = "data-inspector",
    description: str = "Inspects data",
    url: str = "https://data-inspector.workspace.databricksapps.com",
    skills: list[dict] | None = None,
) -> dict:
    return {
        "name": name,
        "description": description,
        "url": url,
        "skills": skills
        or [{"id": "inspect", "name": "inspect", "description": "Inspect a table"}],
    }


def make_responses_payload(text: str) -> dict:
    return {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }


def make_httpx_response(data: dict, status_code: int = 200) -> MagicMock:
    """Build a mock httpx Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data)
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    return resp


# ---------------------------------------------------------------------------
# _url_to_app_name
# ---------------------------------------------------------------------------


class TestUrlToAppName:
    def test_returns_none_for_non_databricks_url(self):
        assert _url_to_app_name("https://example.com/path") is None

    def test_returns_none_for_empty_string(self):
        assert _url_to_app_name("") is None

    def test_workspace_subdomain_pattern(self):
        url = "https://data-inspector.workspace.databricksapps.com"
        result = _url_to_app_name(url)
        # No long numeric suffix → returns full first segment
        assert result == "data-inspector"

    def test_strips_long_numeric_suffix(self):
        # Pattern: <app-name>-<workspace-id>.cloud.databricksapps.com
        # workspace-id is a long numeric string (>8 digits)
        url = "https://myapp-1234567890.cloud.databricksapps.com"
        result = _url_to_app_name(url)
        assert result == "myapp"

    def test_preserves_hyphens_in_app_name(self):
        url = "https://my-cool-app-1234567890.cloud.databricksapps.com"
        result = _url_to_app_name(url)
        assert result == "my-cool-app"

    def test_does_not_strip_short_numeric_suffix(self):
        # Short numeric segments (≤8 digits) are part of the app name
        url = "https://app-123.workspace.databricksapps.com"
        result = _url_to_app_name(url)
        assert result == "app-123"

    def test_returns_none_for_none_url(self):
        assert _url_to_app_name(None) is None  # type: ignore[arg-type]

    def test_handles_url_with_path(self):
        url = "https://data-inspector.workspace.databricksapps.com/.well-known/agent.json"
        result = _url_to_app_name(url)
        assert result == "data-inspector"


# ---------------------------------------------------------------------------
# RemoteDatabricksAgent — construction
# ---------------------------------------------------------------------------


class TestRemoteDatabricksAgentConstruction:
    def test_strips_well_known_suffix_from_card_url(self):
        card_url = "https://data-inspector.workspace.databricksapps.com/.well-known/agent.json"
        agent = RemoteDatabricksAgent(card_url)
        assert agent._base_url == "https://data-inspector.workspace.databricksapps.com"

    def test_infers_app_name_from_base_url(self):
        card_url = "https://data-inspector.workspace.databricksapps.com/.well-known/agent.json"
        agent = RemoteDatabricksAgent(card_url)
        assert agent.app_name == "data-inspector"

    def test_explicit_app_name_overrides_inferred(self):
        card_url = "https://data-inspector.workspace.databricksapps.com/.well-known/agent.json"
        agent = RemoteDatabricksAgent(card_url, app_name="custom-name")
        assert agent.app_name == "custom-name"

    def test_card_is_none_before_init(self):
        agent = RemoteDatabricksAgent("https://host/.well-known/agent.json")
        assert agent.card is None

    def test_name_returns_remote_agent_before_init(self):
        agent = RemoteDatabricksAgent("https://host/.well-known/agent.json")
        assert agent.name == "remote-agent"

    def test_description_returns_empty_string_before_init(self):
        agent = RemoteDatabricksAgent("https://host/.well-known/agent.json")
        assert agent.description == ""

    def test_collect_tools_returns_empty_list(self):
        agent = RemoteDatabricksAgent("https://host/.well-known/agent.json")
        assert agent.collect_tools() == []


# ---------------------------------------------------------------------------
# from_card_url
# ---------------------------------------------------------------------------


class TestFromCardUrl:
    @pytest.mark.asyncio
    async def test_fetches_card_and_returns_agent(self):
        card_data = make_card_data()

        async def mock_get(url, **kwargs):
            return make_httpx_response(card_data)

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(side_effect=mock_get)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            card_url = "https://data-inspector.workspace.databricksapps.com/.well-known/agent.json"
            agent = await RemoteDatabricksAgent.from_card_url(card_url)

        assert agent.card is not None
        assert agent.name == "data-inspector"
        assert agent.description == "Inspects data"

    @pytest.mark.asyncio
    async def test_populates_skills_from_card(self):
        card_data = make_card_data(
            skills=[
                {"id": "s1", "name": "skill-one", "description": "First skill"},
                {"id": "s2", "name": "skill-two", "description": "Second skill"},
            ]
        )

        async def mock_get(url, **kwargs):
            return make_httpx_response(card_data)

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(side_effect=mock_get)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            agent = await RemoteDatabricksAgent.from_card_url(
                "https://host/.well-known/agent.json"
            )

        assert len(agent.card.skills) == 2
        assert agent.card.skills[0].name == "skill-one"

    @pytest.mark.asyncio
    async def test_updates_base_url_from_card(self):
        card_data = make_card_data(url="https://canonical.workspace.databricksapps.com")

        async def mock_get(url, **kwargs):
            return make_httpx_response(card_data)

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(side_effect=mock_get)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            agent = await RemoteDatabricksAgent.from_card_url(
                "https://other-host/.well-known/agent.json"
            )

        assert agent._base_url == "https://canonical.workspace.databricksapps.com"

    @pytest.mark.asyncio
    async def test_raises_on_card_fetch_failure(self):
        async def mock_get(url, **kwargs):
            resp = make_httpx_response({}, status_code=404)
            return resp

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(side_effect=mock_get)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(httpx.HTTPStatusError):
                await RemoteDatabricksAgent.from_card_url(
                    "https://host/.well-known/agent.json"
                )

    @pytest.mark.asyncio
    async def test_init_is_idempotent(self):
        card_data = make_card_data()
        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            return make_httpx_response(card_data)

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(side_effect=mock_get)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            agent = await RemoteDatabricksAgent.from_card_url(
                "https://host/.well-known/agent.json"
            )
            await agent.init()
            await agent.init()

        assert call_count == 1


# ---------------------------------------------------------------------------
# from_app_name
# ---------------------------------------------------------------------------


class TestFromAppName:
    @pytest.mark.asyncio
    async def test_constructs_card_url_from_databricks_host(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", "https://my-workspace.databricks.com")

        fetched_urls: list[str] = []

        async def mock_get(url, **kwargs):
            fetched_urls.append(str(url))
            return make_httpx_response(make_card_data())

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(side_effect=mock_get)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            await RemoteDatabricksAgent.from_app_name("my-app")

        assert fetched_urls[0] == (
            "https://my-workspace.databricks.com/apps/my-app/.well-known/agent.json"
        )

    @pytest.mark.asyncio
    async def test_strips_trailing_slash_from_host(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", "https://my-workspace.databricks.com/")

        fetched_urls: list[str] = []

        async def mock_get(url, **kwargs):
            fetched_urls.append(str(url))
            return make_httpx_response(make_card_data())

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(side_effect=mock_get)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            await RemoteDatabricksAgent.from_app_name("my-app")

        # Trailing slash from host is stripped — no double-slash in the URL
        assert "//" not in fetched_urls[0].replace("https://", "")
        assert fetched_urls[0].endswith("my-app/.well-known/agent.json")

    @pytest.mark.asyncio
    async def test_raises_when_databricks_host_not_set(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_HOST", raising=False)
        with pytest.raises(ValueError, match="DATABRICKS_HOST"):
            await RemoteDatabricksAgent.from_app_name("my-app")

    @pytest.mark.asyncio
    async def test_raises_when_databricks_host_is_empty(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", "")
        with pytest.raises(ValueError, match="DATABRICKS_HOST"):
            await RemoteDatabricksAgent.from_app_name("my-app")

    @pytest.mark.asyncio
    async def test_explicit_app_name_is_set(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", "https://host.databricks.com")

        async def mock_get(url, **kwargs):
            return make_httpx_response(make_card_data(url="https://host.databricks.com/apps/my-app"))

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(side_effect=mock_get)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            agent = await RemoteDatabricksAgent.from_app_name("my-app")

        assert agent.app_name == "my-app"


# ---------------------------------------------------------------------------
# run() — SDK path and HTTP fallback
# ---------------------------------------------------------------------------


class TestRun:
    """Tests for run() method covering DatabricksOpenAI and HTTP paths."""

    def _make_agent_with_card(
        self,
        app_name: str = "data-inspector",
        base_url: str = "https://data-inspector.workspace.databricksapps.com",
    ) -> RemoteDatabricksAgent:
        """Build a pre-initialised agent without making network calls."""
        from apx_agent._models import AgentCard, A2ASkill

        agent = RemoteDatabricksAgent.__new__(RemoteDatabricksAgent)
        agent._card_url = f"{base_url}/.well-known/agent.json"
        agent._base_url = base_url
        agent._app_name = app_name
        agent._extra_headers = {}
        agent._timeout = 120.0
        agent._card = AgentCard(
            name="data-inspector",
            description="Inspects data",
            url=base_url,
            skills=[
                A2ASkill(id="inspect", name="inspect", description="Inspect a table")
            ],
        )
        return agent

    @pytest.mark.asyncio
    async def test_run_calls_sdk_when_app_name_is_set(self):
        agent = self._make_agent_with_card()
        request = make_request()

        mock_response = MagicMock()
        mock_response.output_text = "SDK result"

        with patch("databricks_openai.AsyncDatabricksOpenAI") as MockSDK:
            sdk_instance = AsyncMock()
            sdk_instance.responses.create = AsyncMock(return_value=mock_response)
            MockSDK.return_value = sdk_instance

            result = await agent.run([Message(role="user", content="Hello")], request)

        assert result == "SDK result"
        sdk_instance.responses.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_calls_sdk_with_correct_model(self):
        agent = self._make_agent_with_card(app_name="my-app")
        request = make_request()

        mock_response = MagicMock()
        mock_response.output_text = "ok"

        with patch("databricks_openai.AsyncDatabricksOpenAI") as MockSDK:
            sdk_instance = AsyncMock()
            sdk_instance.responses.create = AsyncMock(return_value=mock_response)
            MockSDK.return_value = sdk_instance

            await agent.run([Message(role="user", content="hi")], request)

            call_kwargs = sdk_instance.responses.create.call_args
            assert call_kwargs.kwargs.get("model") == "apps/my-app"

    @pytest.mark.asyncio
    async def test_run_falls_back_to_http_when_sdk_raises(self):
        agent = self._make_agent_with_card()
        request = make_request()

        payload = make_responses_payload("HTTP fallback result")

        with patch("databricks_openai.AsyncDatabricksOpenAI") as MockSDK:
            sdk_instance = AsyncMock()
            sdk_instance.responses.create = AsyncMock(
                side_effect=Exception("SDK unavailable")
            )
            MockSDK.return_value = sdk_instance

            async def mock_post(url, **kwargs):
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.json.return_value = payload
                resp.raise_for_status = MagicMock()
                return resp

            with patch("httpx.AsyncClient") as MockClient:
                instance = AsyncMock()
                instance.post = AsyncMock(side_effect=mock_post)
                MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
                MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

                result = await agent.run(
                    [Message(role="user", content="Hello")], request
                )

        assert result == "HTTP fallback result"

    @pytest.mark.asyncio
    async def test_run_uses_http_directly_when_no_app_name(self):
        agent = self._make_agent_with_card(app_name=None)
        agent._app_name = None
        request = make_request()

        payload = make_responses_payload("Direct HTTP result")

        async def mock_post(url, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = payload
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post = AsyncMock(side_effect=mock_post)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await agent.run(
                [Message(role="user", content="Hello")], request
            )

        assert result == "Direct HTTP result"

    @pytest.mark.asyncio
    async def test_http_path_sends_messages_in_correct_format(self):
        agent = self._make_agent_with_card()
        agent._app_name = None  # force HTTP path
        request = make_request()

        received_body: dict = {}

        async def mock_post(url, **kwargs):
            received_body.update(kwargs.get("json", {}))
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = make_responses_payload("ok")
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post = AsyncMock(side_effect=mock_post)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            messages = [
                Message(role="user", content="Question"),
                Message(role="assistant", content="Answer"),
            ]
            await agent.run(messages, request)

        assert received_body["input"] == [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer"},
        ]

    @pytest.mark.asyncio
    async def test_http_path_raises_on_error_status(self):
        agent = self._make_agent_with_card()
        agent._app_name = None
        request = make_request()

        async def mock_post(url, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 500
            resp.text = "Internal Server Error"
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post = AsyncMock(side_effect=mock_post)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(RuntimeError, match="500"):
                await agent.run([Message(role="user", content="hi")], request)

    @pytest.mark.asyncio
    async def test_http_path_json_fallback_on_unexpected_shape(self):
        agent = self._make_agent_with_card()
        agent._app_name = None
        request = make_request()

        async def mock_post(url, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {"output": []}  # unexpected shape
            resp.raise_for_status = MagicMock()
            return resp

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post = AsyncMock(side_effect=mock_post)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await agent.run([Message(role="user", content="hi")], request)

        # Falls back to JSON.dumps — should not raise
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# OBO header forwarding
# ---------------------------------------------------------------------------


class TestOboHeaders:
    def _make_agent(self) -> RemoteDatabricksAgent:
        from apx_agent._models import AgentCard, A2ASkill

        agent = RemoteDatabricksAgent.__new__(RemoteDatabricksAgent)
        agent._card_url = "https://host/.well-known/agent.json"
        agent._base_url = "https://host"
        agent._app_name = None
        agent._extra_headers = {}
        agent._timeout = 120.0
        agent._card = AgentCard(name="host", description="", url="https://host", skills=[])
        return agent

    def test_extracts_authorization_header(self):
        agent = self._make_agent()
        request = make_request({"Authorization": "Bearer token123"})
        headers = agent._obo_headers(request)
        assert headers["Authorization"] == "Bearer token123"

    def test_extracts_forwarded_access_token(self):
        agent = self._make_agent()
        request = make_request({"X-Forwarded-Access-Token": "obo-token"})
        headers = agent._obo_headers(request)
        assert headers["X-Forwarded-Access-Token"] == "obo-token"

    def test_extracts_forwarded_host(self):
        agent = self._make_agent()
        request = make_request({"X-Forwarded-Host": "original-host.com"})
        headers = agent._obo_headers(request)
        assert headers["X-Forwarded-Host"] == "original-host.com"

    def test_omits_missing_obo_headers(self):
        agent = self._make_agent()
        request = make_request({})
        headers = agent._obo_headers(request)
        assert "Authorization" not in headers
        assert "X-Forwarded-Access-Token" not in headers

    def test_merges_extra_headers_with_obo_headers(self):
        agent = self._make_agent()
        agent._extra_headers = {"X-Custom": "custom-val"}
        request = make_request({"Authorization": "Bearer tok"})
        headers = agent._obo_headers(request)
        assert headers["X-Custom"] == "custom-val"
        assert headers["Authorization"] == "Bearer tok"

    def test_obo_headers_override_extra_headers_on_conflict(self):
        agent = self._make_agent()
        agent._extra_headers = {"Authorization": "Bearer extra"}
        request = make_request({"Authorization": "Bearer obo"})
        headers = agent._obo_headers(request)
        # Request headers override extra headers
        assert headers["Authorization"] == "Bearer obo"


# ---------------------------------------------------------------------------
# fetch_remote_tools
# ---------------------------------------------------------------------------


class TestFetchRemoteTools:
    def _make_agent_with_skills(self) -> RemoteDatabricksAgent:
        from apx_agent._models import AgentCard, A2ASkill

        agent = RemoteDatabricksAgent.__new__(RemoteDatabricksAgent)
        agent._card_url = "https://host/.well-known/agent.json"
        agent._base_url = "https://host"
        agent._app_name = "host"
        agent._extra_headers = {}
        agent._timeout = 120.0
        agent._card = AgentCard(
            name="host",
            description="desc",
            url="https://host",
            skills=[
                A2ASkill(id="skill-one", name="skill-one", description="Skill one"),
                A2ASkill(id="skill-two", name="skill-two", description="Skill two"),
            ],
        )
        return agent

    @pytest.mark.asyncio
    async def test_returns_one_tool_per_skill(self):
        agent = self._make_agent_with_skills()
        tools = await agent.fetch_remote_tools()
        assert len(tools) == 2

    @pytest.mark.asyncio
    async def test_tool_names_are_derived_from_skill_names(self):
        agent = self._make_agent_with_skills()
        tools = await agent.fetch_remote_tools()
        names = {t.name for t in tools}
        assert "skill_one" in names
        assert "skill_two" in names

    @pytest.mark.asyncio
    async def test_tool_sub_agent_url_points_to_base_url(self):
        agent = self._make_agent_with_skills()
        tools = await agent.fetch_remote_tools()
        for tool in tools:
            assert tool.sub_agent_url == "https://host"

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_card(self):
        agent = RemoteDatabricksAgent.__new__(RemoteDatabricksAgent)
        agent._card_url = "https://host/.well-known/agent.json"
        agent._base_url = "https://host"
        agent._app_name = None
        agent._extra_headers = {}
        agent._timeout = 120.0
        agent._card = None

        # Stub init() so it does not attempt to fetch the card over the network;
        # the guard inside fetch_remote_tools checks self._card after init().
        async def _no_op_init():
            pass

        agent.init = _no_op_init  # type: ignore[method-assign]

        tools = await agent.fetch_remote_tools()
        assert tools == []
