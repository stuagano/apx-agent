import json

import pytest
import respx
import httpx

from integrations.jira.jira_client import JiraClient, JiraClientError


BASE = "https://test.atlassian.net"


@respx.mock
def test_post_comment_success():
    route = respx.post(f"{BASE}/rest/api/3/issue/DATA-42/comment").mock(
        return_value=httpx.Response(201, json={"id": "10001"})
    )
    client = JiraClient(BASE, "bot@test.com", "tok")
    client.post_comment("DATA-42", "Investigation complete.")
    assert route.called


@respx.mock
def test_post_comment_raises_on_error():
    respx.post(f"{BASE}/rest/api/3/issue/DATA-99/comment").mock(
        return_value=httpx.Response(403, json={"errorMessages": ["Forbidden"]})
    )
    client = JiraClient(BASE, "bot@test.com", "tok")
    with pytest.raises(JiraClientError, match="403"):
        client.post_comment("DATA-99", "body")


@respx.mock
def test_post_comment_sends_adf_body():
    captured = {}

    def capture(request, route):
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={})

    respx.post(f"{BASE}/rest/api/3/issue/DATA-1/comment").mock(side_effect=capture)
    client = JiraClient(BASE, "bot@test.com", "tok")
    client.post_comment("DATA-1", "hello world")

    assert captured["body"]["body"]["type"] == "doc"
    assert captured["body"]["body"]["content"][0]["content"][0]["text"] == "hello world"


@respx.mock
def test_trailing_slash_normalized():
    route = respx.post(f"{BASE}/rest/api/3/issue/DATA-5/comment").mock(
        return_value=httpx.Response(201, json={})
    )
    client = JiraClient(BASE + "/", "bot@test.com", "tok")  # trailing slash
    client.post_comment("DATA-5", "test")
    assert route.called
