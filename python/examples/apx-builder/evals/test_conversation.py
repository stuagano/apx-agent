"""
Tier 1 conversation quality eval for apx-builder.

Walks through the 5-step discovery flow with scripted answers and judges each
agent response for: one question at a time, no technical jargon, correct
progression. Stops before triggering the build phase (no scaffold/deploy).

Usage:
    APP_URL=https://apx-builder-....databricksapps.com \\
    DATABRICKS_HOST=https://your-workspace.cloud.databricks.com \\
    DATABRICKS_TOKEN=<oauth-access-token> \\
    pytest evals/test_conversation.py -v -m integration
"""
from __future__ import annotations

import os
import httpx
import pytest

APP_URL = os.environ.get("APP_URL", "").rstrip("/")
DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
DATABRICKS_TOKEN = os.environ.get("DATABRICKS_TOKEN", "")

# Five discovery turns that exhaust all questions without triggering the build.
# Turn 5 answers the lineage question; the agent should then ask for a name.
# We stop there — providing the name would start scaffold/deploy/poll.
_SCRIPT = [
    "I want to build an agent",
    "It should help our sales team answer questions about revenue by region",
    "Use main.sales.transactions and main.sales.customers",
    "No Genie spaces",
    "No lineage",
]

_CRITERIA = [
    (
        "Asks exactly one question about what the agent should do. "
        "Does not mention Python, SQL, code, workspace paths, pyproject.toml, or any technical terms."
    ),
    (
        "Asks about which data sources or tables to use, in plain English. "
        "One question only. No jargon."
    ),
    (
        "Asks about whether the agent should connect to any Genie spaces. "
        "One question only. No technical jargon, no code, no file paths."
    ),
    (
        "Asks about data lineage in plain English. One question only."
    ),
    (
        "Asks for a name for the agent and suggests an example (lowercase letters and hyphens, no spaces)."
    ),
]


def _get_token() -> str:
    if DATABRICKS_TOKEN:
        return DATABRICKS_TOKEN
    # Fall back to CLI token
    import subprocess, json
    result = subprocess.run(
        ["databricks", "auth", "token", "--profile", "fe-stable"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return json.loads(result.stdout)["access_token"]
    pytest.skip("No DATABRICKS_TOKEN and databricks CLI token unavailable")


def _chat(messages: list[dict], session_id: str | None = None) -> tuple[str, str | None]:
    """Send a conversation to the app and return (response_text, session_id)."""
    token = _get_token()
    payload: dict = {"input": messages}
    if session_id:
        payload["session_id"] = session_id
    r = httpx.post(
        f"{APP_URL}/responses",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=240.0,
    )
    r.raise_for_status()
    data = r.json()
    try:
        text = data["output"][0]["content"][0]["text"]
    except (KeyError, IndexError):
        text = str(data)
    return text, data.get("session_id")


def _judge(user_msg: str, agent_response: str, criterion: str) -> tuple[bool, str]:
    """LLM-as-judge via Databricks chat completions. Returns (passed, reason)."""
    import time

    host = DATABRICKS_HOST
    if not host:
        pytest.skip("DATABRICKS_HOST not set — cannot run LLM judge")

    token = _get_token()
    prompt = (
        "You are evaluating whether an AI assistant's response meets a criterion.\n\n"
        f"User message:\n{user_msg}\n\n"
        f"Assistant response:\n{agent_response}\n\n"
        f"Criterion:\n{criterion}\n\n"
        "Reply with exactly two lines:\n"
        "VERDICT: PASS or FAIL\n"
        "REASON: one sentence"
    )
    for attempt in range(5):
        r = httpx.post(
            f"{host}/serving-endpoints/databricks-claude-sonnet-4-6/invocations",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"messages": [{"role": "user", "content": prompt}], "max_tokens": 120},
            timeout=30.0,
        )
        if r.status_code == 429:
            wait = 15 * (attempt + 1)
            time.sleep(wait)
            continue
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
        passed = "VERDICT: PASS" in text
        reason = text.split("REASON:", 1)[-1].strip() if "REASON:" in text else text
        return passed, reason
    r.raise_for_status()
    return False, "judge rate-limited after 5 retries"


@pytest.fixture(scope="module")
def conversation():
    """Walk through the discovery script once and return all (user, agent) turn pairs."""
    if not APP_URL:
        pytest.skip("APP_URL not set")
    history: list[dict] = []
    turns: list[tuple[str, str]] = []
    sid: str | None = None
    for user_msg in _SCRIPT:
        history.append({"role": "user", "content": user_msg})
        agent_response, sid = _chat(history, sid)
        history.append({"role": "assistant", "content": agent_response})
        turns.append((user_msg, agent_response))
    return turns


@pytest.mark.integration
@pytest.mark.parametrize("turn_index,label", [
    (0, "asks_use_case"),
    (1, "asks_data_sources"),
    (2, "confirms_tables_asks_next"),
    (3, "asks_lineage"),
    (4, "asks_for_name"),
])
def test_discovery_turn(conversation, turn_index, label):
    user_msg, agent_response = conversation[turn_index]
    criterion = _CRITERIA[turn_index]
    passed, reason = _judge(user_msg, agent_response, criterion)
    assert passed, (
        f"Turn {turn_index + 1} ({label}) failed judge.\n"
        f"Criterion: {criterion}\n"
        f"Reason: {reason}\n"
        f"Agent said: {agent_response!r}"
    )
