"""Tests for the /responses FastAPI endpoint."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport


@pytest.mark.asyncio
async def test_responses_returns_correct_output_format():
    """/responses returns output with expected shape and session_id."""
    from app import app

    mock_text = "What should your agent do?"
    mock_session_id = "sess_abc123"

    with patch("app.get_mcp_servers") as mock_servers, \
         patch("app.asyncio.to_thread") as mock_to_thread, \
         patch("app.set_databricks_auth"), \
         patch("app.clear_databricks_auth"), \
         patch("app._collect_result") as mock_collect, \
         patch("app.asyncio.get_event_loop") as mock_loop:

        mock_servers.return_value = ({}, ["mcp__apx__create_and_deploy_app"])

        # Simulate the user email lookup
        mock_to_thread.return_value = "user@example.com"

        # Simulate the queue result
        mock_q = MagicMock()
        mock_q.get.return_value = ("done", (mock_text, mock_session_id))

        import queue as queue_module
        mock_loop_instance = MagicMock()
        mock_loop.return_value = mock_loop_instance

        async def fake_run_in_executor(_, fn):
            return fn()

        mock_loop_instance.run_in_executor = fake_run_in_executor

        with patch("app.queue.Queue", return_value=mock_q), \
             patch("app.threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/responses",
                    json={"input": [{"role": "user", "content": "I want to build an agent"}]},
                    headers={"Authorization": "Bearer fake-token"},
                )

    assert response.status_code == 200
    data = response.json()
    assert "output" in data
    assert data["output"][0]["type"] == "message"
    assert data["output"][0]["content"][0]["text"] == mock_text
    assert data["session_id"] == mock_session_id


@pytest.mark.asyncio
async def test_responses_returns_400_for_empty_input():
    """/responses returns 400 when input is empty."""
    from app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/responses",
            json={"input": []},
            headers={"Authorization": "Bearer fake-token"},
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_responses_passes_session_id_for_resumption():
    """/responses passes session_id to ClaudeAgentOptions.resume when provided."""
    from app import app

    options_captured = {}

    with patch("app.get_mcp_servers", return_value=({}, [])), \
         patch("app.asyncio.to_thread", return_value="user@example.com"), \
         patch("app.set_databricks_auth"), \
         patch("app.clear_databricks_auth"), \
         patch("app.get_system_prompt", return_value="system prompt"), \
         patch("app.ClaudeAgentOptions") as mock_opts_cls, \
         patch("app.asyncio.get_event_loop") as mock_loop:

        mock_opts_cls.side_effect = lambda **kw: (options_captured.update(kw), MagicMock())[1]

        mock_q = MagicMock()
        mock_q.get.return_value = ("done", ("hello", "sess_new"))

        mock_loop_instance = MagicMock()
        mock_loop.return_value = mock_loop_instance

        async def fake_run_in_executor(_, fn):
            return fn()

        mock_loop_instance.run_in_executor = fake_run_in_executor

        with patch("app.queue.Queue", return_value=mock_q), \
             patch("app.threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await client.post(
                    "/responses",
                    json={
                        "input": [{"role": "user", "content": "Hello"}],
                        "session_id": "sess_existing",
                    },
                    headers={"Authorization": "Bearer fake-token"},
                )

    assert options_captured.get("resume") == "sess_existing"
