"""Tests for the /responses FastAPI endpoint."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport


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
async def test_discovery_first_turn_returns_question_one():
    """First message triggers Question 1 without calling the LLM."""
    from app import app, _sessions

    # Clear any leftover state
    _sessions.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/responses",
            json={"input": [{"role": "user", "content": "I want to build an agent"}]},
            headers={"Authorization": "Bearer fake-token"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["output"][0]["content"][0]["text"] == "What should your agent do?"
    assert data["session_id"]  # a UUID was assigned


@pytest.mark.asyncio
async def test_discovery_advances_through_five_questions():
    """All 5 questions are asked in order without LLM involvement."""
    from app import app, _sessions, QUESTIONS

    _sessions.clear()

    expected_questions = list(QUESTIONS)
    responses_seen = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sid = None
        for user_answer in ["I want to build an agent"] + ["answer"] * (len(QUESTIONS) - 1):
            payload = {"input": [{"role": "user", "content": user_answer}]}
            if sid:
                payload["session_id"] = sid
            r = await client.post(
                "/responses",
                json=payload,
                headers={"Authorization": "Bearer fake-token"},
            )
            assert r.status_code == 200
            data = r.json()
            sid = data["session_id"]
            responses_seen.append(data["output"][0]["content"][0]["text"])

    assert responses_seen == expected_questions


@pytest.mark.asyncio
async def test_fifth_answer_returns_payload_confirmation():
    """After all 5 answers, the server returns a payload summary with build_pending=True."""
    from app import app, _sessions, QUESTIONS

    _sessions.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sid = None
        for user_answer in ["I want to build an agent"] + ["answer"] * (len(QUESTIONS) - 1):
            payload = {"input": [{"role": "user", "content": user_answer}]}
            if sid:
                payload["session_id"] = sid
            r = await client.post(
                "/responses",
                json=payload,
                headers={"Authorization": "Bearer fake-token"},
            )
            sid = r.json()["session_id"]

        # 6th message (5th answer) → payload confirmation, not LLM call
        r = await client.post(
            "/responses",
            json={"input": [{"role": "user", "content": "sales-assistant"}], "session_id": sid},
            headers={"Authorization": "Bearer fake-token"},
        )

    assert r.status_code == 200
    data = r.json()
    assert data.get("build_pending") is True
    text = data["output"][0]["content"][0]["text"]
    assert "mcp-sales-assistant" in text


@pytest.mark.asyncio
async def test_build_phase_invokes_sdk_after_confirmation():
    """The turn after the payload confirmation (build_pending) calls the Claude Code SDK."""
    import asyncio as _asyncio
    from app import app, _sessions, QUESTIONS

    _sessions.clear()

    build_text = "Your agent is deploying at https://example.databricksapps.com"

    real_loop = _asyncio.get_running_loop()

    def fake_run_in_executor(_executor, fn):
        fut = real_loop.create_future()
        fut.set_result(fn())
        return fut

    mock_loop = MagicMock()
    mock_loop.run_in_executor = fake_run_in_executor

    async def fake_to_thread(fn, *args, **kwargs):
        return fn()

    with patch("app.asyncio.to_thread", side_effect=fake_to_thread), \
         patch("app.asyncio.get_running_loop", return_value=mock_loop), \
         patch("app.get_mcp_servers", return_value=({}, [])), \
         patch("app.set_databricks_auth"), \
         patch("app.clear_databricks_auth"), \
         patch("app.get_build_prompt", return_value="build prompt"), \
         patch("app.threading.Thread") as mock_thread, \
         patch("app._get_from_queue", return_value=("done", (build_text, "sdk_session"))):

        mock_thread.return_value.start = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            sid = None
            for user_answer in ["I want to build an agent"] + ["answer"] * (len(QUESTIONS) - 1):
                payload = {"input": [{"role": "user", "content": user_answer}]}
                if sid:
                    payload["session_id"] = sid
                r = await client.post(
                    "/responses",
                    json=payload,
                    headers={"Authorization": "Bearer fake-token"},
                )
                sid = r.json()["session_id"]

            # 6th message → payload confirmation
            r = await client.post(
                "/responses",
                json={"input": [{"role": "user", "content": "sales-assistant"}], "session_id": sid},
                headers={"Authorization": "Bearer fake-token"},
            )
            assert r.json().get("build_pending") is True

            # 7th message (auto-trigger, user-messages-only) → actual LLM build
            r = await client.post(
                "/responses",
                json={
                    "input": [{"role": "user", "content": "sales-assistant"}],
                    "session_id": sid,
                },
                headers={"Authorization": "Bearer fake-token"},
            )

    assert r.status_code == 200
    assert r.json()["output"][0]["content"][0]["text"] == build_text
